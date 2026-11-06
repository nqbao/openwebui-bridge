import asyncio
import base64
import hmac
import json
import logging
import os
import time
import uuid
from contextlib import aclosing, asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from mcp.server.mcpserver.exceptions import ToolError

from .backends import ClaudeBackend, CodexBackend, Done, Image, Text
from .config import Profile, Settings
from .images import ImageDeliveryError, validate_image
from .openwebui_mcp import OpenWebUIClient, connection_env
from .protocol import CompletionRequest, digest, replay, uploaded_image_ids
from .store import ChatLocks, Store

logger = logging.getLogger(__name__)


def task_label(request: Request) -> str:
    task = request.headers.get("x-openwebui-task", "").strip()
    if not task:
        return "unspecified"
    # Task names are diagnostic metadata; never log arbitrary header values.
    known = {
        "title_generation",
        "tags_generation",
        "follow_up_generation",
        "query_generation",
        "autocomplete_generation",
        "emoji_generation",
        "image_prompt_generation",
        "moa_response_generation",
        "voice_mode_prompt_generation",
    }
    return task if task in known else "other"


def error_payload(message: str, kind: str = "invalid_request_error") -> dict:
    return {"error": {"message": message, "type": kind, "param": None, "code": None}}


def sse(value: dict) -> str:
    return "data: " + json.dumps(value, ensure_ascii=False) + "\n\n"


def usage_payload(usage: dict) -> dict:
    prompt_tokens = sum(
        usage.get(k, 0)
        for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    )
    output_tokens = usage.get("output_tokens", 0)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": prompt_tokens + output_tokens,
    }


def create_app(settings: Settings, backends: dict | None = None) -> FastAPI:
    locks = ChatLocks()
    backends = backends or {"claude": ClaudeBackend(), "codex": CodexBackend()}

    @asynccontextmanager
    async def lifespan(app):
        app.state.store = Store(settings.database)
        try:
            yield
        finally:
            app.state.store.close()

    app = FastAPI(title="Open WebUI Bridge", lifespan=lifespan)

    @app.exception_handler(HTTPException)
    async def http_error(_request, exc):
        return JSONResponse(error_payload(str(exc.detail)), status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, exc):
        # Do not echo the user's prompt in validation errors.
        messages = [f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in exc.errors()]
        reason = "; ".join(messages)
        if _request.url.path == "/v1/chat/completions":
            body = exc.body if isinstance(exc.body, dict) else {}
            stream = body.get("stream", False)
            # Log only the stream flag and validation reason, never the body or
            # headers. stream=false suggests a WebUI task but is not proof.
            logger.warning(
                "Rejected completion request: stream=%s; task=%s; %s",
                stream if isinstance(stream, bool) else "invalid",
                task_label(_request),
                reason,
            )
        return JSONResponse(error_payload(reason), status_code=400)

    async def authenticate(request: Request):
        if settings.api_key:
            expected = "Bearer " + settings.api_key
            if not hmac.compare_digest(request.headers.get("authorization", ""), expected):
                raise HTTPException(401, "Invalid proxy API key")

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/v1/models", dependencies=[Depends(authenticate)])
    async def models():
        return {
            "object": "list",
            "data": [
                {"id": name, "object": "model", "created": 0, "owned_by": "openwebui-bridge"}
                for name in settings.profiles
            ],
        }

    @app.post("/v1/chat/completions", dependencies=[Depends(authenticate)])
    async def completion(body: CompletionRequest, request: Request):
        profile = settings.profiles.get(body.model)
        if profile is None:
            raise HTTPException(404, f"Unknown agent profile: {body.model}")
        # An exactly empty header is the explicit opt-in to foreground chat for
        # non-streaming clients; any other value (including whitespace) is a task.
        task_header = request.headers.get("x-openwebui-task")
        isolated = (task_header is None and not body.stream) or bool(task_header)
        timeout = settings.turn_timeout_seconds
        if isolated:
            # Claude's SDK can expose an empty tool set. Codex's CLI does not
            # provide the same contract, so task calls consistently use Claude.
            profile = Profile(
                backend="claude",
                cwd=profile.cwd,
                model=settings.task_model,
                allowed_tools=[],
                project_instructions=False,
                max_turns=2,
                system_prompt="Complete the standalone text task using only the supplied "
                "conversation. Return exactly the requested output. Do not use tools or "
                "persist memory. You are not working on a repository task.",
            )
            timeout = settings.task_timeout_seconds
            logger.info("Isolated completion: stream=%s; task=%s", body.stream, task_label(request))
        messages = [message.canonical() for message in body.messages]
        try:
            # Changing the configured API identity must not reuse another user's
            # native session. Store only a digest, never the API key itself.
            integration_identity = digest(connection_env(profile.openwebui, profile.mcp_servers))
        except ValueError as exc:
            raise HTTPException(503, str(exc)) from None
        chat_id = request.headers.get("x-openwebui-chat-id")
        user_id = request.headers.get("x-openwebui-user-id", "local")
        # Include all configuration: changed tools/model/system prompt start fresh.
        key = (
            digest(
                [
                    user_id,
                    chat_id,
                    body.model,
                    profile.model_dump(mode="json"),
                    integration_identity,
                ]
            )
            if chat_id and not isolated
            else None
        )
        message_ids = {}
        if key:
            for name, header in (
                ("user_message_id", "x-openwebui-user-message-id"),
                ("assistant_message_id", "x-openwebui-message-id"),
                ("parent_message_id", "x-openwebui-user-message-parent-id"),
            ):
                value = request.headers.get(header)
                if value is not None:
                    value = value.strip()
                    if len(value) > 200 or any(ord(char) < 32 for char in value):
                        raise HTTPException(400, "Invalid message-ID header")
                    message_ids[name] = value or None
            message_ids["parent_known"] = "x-openwebui-user-message-parent-id" in request.headers
        response_id = "chatcmpl-" + uuid.uuid4().hex
        created = int(time.time())

        async def upload_url(event: Image) -> str | None:
            # Prefer Open WebUI's file store (small messages, owned by Open WebUI);
            # any problem falls back to inline base64 so the image is never lost.
            config = profile.openwebui
            token = os.environ.get(config.api_key_env, "") if config else ""
            if not (config and config.upload_images and token):
                return None
            try:
                return await OpenWebUIClient(config, token).upload_image(
                    event.data, event.mime_type
                )
            except ToolError as exc:
                logger.warning("Open WebUI image upload failed; using inline image: %s", exc)
                return None

        async def render(event: Text | Image) -> str:
            if isinstance(event, Text):
                return event.content
            alt = "".join(c for c in (event.alt or "Generated image")[:500] if ord(c) >= 32)
            for char in "\\[]!<>`":
                alt = alt.replace(char, "\\" + char)
            validate_image(event.data, event.mime_type)
            url = await upload_url(event)
            if url is None:
                url = f"data:{event.mime_type};base64," + base64.b64encode(event.data).decode()
            return f"\n\n![{alt}]({url})\n\n"

        async def fetch_uploaded_images() -> dict[str, tuple[bytes, str]]:
            # Only same-origin Open WebUI file links are fetched, via the profile's own
            # connection. Failures leave the link as text instead of failing the turn.
            config = profile.openwebui
            token = os.environ.get(config.api_key_env, "") if config else ""
            ids = uploaded_image_ids(messages)[:20]
            if not (config and token and ids):
                return {}
            client = OpenWebUIClient(config, token)
            files = {}
            for file_id in ids:
                try:
                    files[file_id] = await client.download_image(file_id)
                except ToolError as exc:
                    logger.warning("Could not load stored image for replay: %s", exc)
            return files

        async def prepare_turn():
            store = request.app.state.store
            row = store.get(key) if key else None
            if key and message_ids.get("parent_known"):
                parent = message_ids.get("parent_message_id")
                row = store.get_branch(key, parent) if parent else None
            resume = None
            if (
                row
                and row["cwd"] == str(profile.cwd)
                and row["prefix_hash"] == digest(messages[:-1])
                and not (
                    message_ids.get("user_message_id")
                    and message_ids["user_message_id"] == row["user_message_id"]
                )
                and not (
                    message_ids.get("assistant_message_id")
                    and message_ids["assistant_message_id"] == row["assistant_message_id"]
                )
            ):
                resume = row["session_id"]
            if resume:
                prompt = messages[-1]["content"]
            else:
                prompt = replay(messages, await fetch_uploaded_images())
            if key:
                if resume:
                    store.invalidate_session(key, resume)
                store.delete(key)
            return prompt, resume

        def save_turn(result, rendered):
            if key:
                prefix = messages + [{"role": "assistant", "content": "".join(rendered)}]
                request.app.state.store.save(
                    key, result.session_id, str(profile.cwd), digest(prefix), **message_ids
                )

        if not body.stream:

            async def collect():
                if key:
                    async with locks.hold(key):
                        return await collect_turn()
                return await collect_turn()

            async def collect_turn():
                prompt, resume = await prepare_turn()
                rendered = []
                result = None
                async with (
                    asyncio.timeout(timeout),
                    aclosing(backends[profile.backend].stream(profile, prompt, resume)) as events,
                ):
                    async for event in events:
                        if isinstance(event, (Text, Image)):
                            if result is not None:
                                raise RuntimeError("Backend emitted text after its result")
                            rendered.append(await render(event))
                        elif isinstance(event, Done):
                            if result is not None:
                                raise RuntimeError("Backend emitted duplicate results")
                            result = event
                if result is None:
                    raise RuntimeError("Backend did not return a result")
                save_turn(result, rendered)
                response = {
                    "id": response_id,
                    "object": "chat.completion",
                    "created": created,
                    "model": body.model,
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": "".join(rendered)},
                        }
                    ],
                }
                if result.usage:
                    response["usage"] = usage_payload(result.usage)
                return response

            async def wait_for_disconnect():
                while True:
                    disconnected = await request.is_disconnected()
                    # Starlette probes receive inside its own cancelled scope,
                    # which can swallow an overlapping task.cancel(). Do not
                    # leave this watcher running when a fast collector finishes.
                    if disconnected or asyncio.current_task().cancelling():
                        return
                    await asyncio.sleep(0.1)

            # Keep SDK entry, iteration and cleanup in the same producer task.
            collector = asyncio.create_task(collect())
            disconnect = asyncio.create_task(wait_for_disconnect())
            try:
                finished, _ = await asyncio.wait(
                    {collector, disconnect}, return_when=asyncio.FIRST_COMPLETED
                )
                if disconnect in finished:
                    raise HTTPException(499, "Client disconnected")
                return JSONResponse(await collector)
            except HTTPException:
                raise
            except Exception as exc:  # noqa: BLE001 - translate arbitrary backend failures
                logger.warning("Isolated completion failed (%s)", type(exc).__name__)
                return JSONResponse(
                    error_payload(
                        str(exc)
                        if isinstance(exc, ImageDeliveryError)
                        else "Isolated completion failed; retry the request",
                        "server_error",
                    ),
                    status_code=504 if isinstance(exc, TimeoutError) else 502,
                )
            finally:
                for task in (collector, disconnect):
                    if not task.done():
                        task.cancel()
                cleanup = asyncio.gather(collector, disconnect, return_exceptions=True)
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    await asyncio.shield(cleanup)

        def chunk(delta: dict, finish: str | None = None, choices: bool = True, **extra):
            return sse(
                {
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": body.model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]
                    if choices
                    else [],
                    **extra,
                }
            )

        async def run_turn():
            prompt, resume = await prepare_turn()
            queue: asyncio.Queue = asyncio.Queue(maxsize=64)

            async def produce():
                try:
                    async with asyncio.timeout(timeout):
                        iterator = backends[profile.backend].stream(profile, prompt, resume)
                        try:
                            async for event in iterator:
                                await queue.put(event)
                        finally:
                            await iterator.aclose()
                    await queue.put(None)
                except Exception as exc:  # noqa: BLE001 - translate arbitrary backend failures
                    # Details can contain tool data or local paths. Return a generic
                    # error and log only its class; preserve session invalidation.
                    logger.warning("Agent turn failed (%s)", type(exc).__name__)
                    await queue.put(exc)

            producer = asyncio.create_task(produce())
            rendered: list[str] = []
            result = None
            try:
                yield chunk({"role": "assistant", "content": ""})
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=10)
                    except TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    if event is None:
                        break
                    if isinstance(event, Exception):
                        raise event
                    if isinstance(event, (Text, Image)):
                        if result is not None:
                            raise RuntimeError("Backend emitted text after its result")
                        content = await render(event)
                        rendered.append(content)
                        yield chunk({"content": content})
                    elif isinstance(event, Done):
                        if result is not None:
                            raise RuntimeError("Backend emitted duplicate results")
                        result = event
                if result is None:
                    raise RuntimeError("Backend did not return a session")
                save_turn(result, rendered)
                yield chunk({}, finish="stop")
                if (
                    body.stream_options
                    and body.stream_options.get("include_usage")
                    and result.usage
                ):
                    yield chunk(
                        {},
                        choices=False,
                        usage=usage_payload(result.usage),
                    )
                yield "data: [DONE]\n\n"
            except Exception as exc:  # noqa: BLE001 - HTTP headers are already sent; report via SSE
                yield sse(
                    error_payload(
                        str(exc)
                        if isinstance(exc, ImageDeliveryError)
                        else "Agent turn failed; retry to start a fresh session",
                        "server_error",
                    )
                )
                yield "data: [DONE]\n\n"
            finally:
                producer.cancel()
                # Shield cleanup from Starlette's disconnect cancellation scope.
                cleanup = asyncio.gather(producer, return_exceptions=True)
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    await asyncio.shield(cleanup)

        async def stream():
            if key:
                async with locks.hold(key), aclosing(run_turn()) as turn:
                    async for data in turn:
                        yield data
            else:
                async with aclosing(run_turn()) as turn:
                    async for data in turn:
                        yield data

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return app
