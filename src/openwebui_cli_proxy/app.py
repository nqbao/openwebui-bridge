import asyncio
import hmac
import json
import logging
import time
import uuid
from contextlib import aclosing, asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse

from .backends import ClaudeBackend, CodexBackend, Done, Text
from .config import Profile, Settings
from .protocol import CompletionRequest, digest, replay
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

    app = FastAPI(title="Open WebUI CLI Proxy", lifespan=lifespan)

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
                {"id": name, "object": "model", "created": 0, "owned_by": "cli-proxy"}
                for name in settings.profiles
            ],
        }

    @app.post("/v1/chat/completions", dependencies=[Depends(authenticate)])
    async def completion(body: CompletionRequest, request: Request):
        profile = settings.profiles.get(body.model)
        if profile is None:
            raise HTTPException(404, f"Unknown agent profile: {body.model}")
        isolated = not body.stream or bool(request.headers.get("x-openwebui-task", "").strip())
        timeout = settings.turn_timeout_seconds
        if isolated:
            # Claude's SDK can expose an empty tool set. Codex's CLI does not
            # provide the same contract, so task calls consistently use Claude.
            profile = Profile(
                backend="claude",
                cwd=profile.cwd,
                model=settings.task_model,
                allowed_tools=[],
                max_turns=2,
                system_prompt="Complete the standalone text task using only the supplied "
                "conversation. Return exactly the requested output. Do not use tools or "
                "persist memory. You are not working on a repository task.",
            )
            timeout = settings.task_timeout_seconds
            logger.info("Isolated completion: stream=%s; task=%s", body.stream, task_label(request))
        messages = [message.canonical() for message in body.messages]
        chat_id = request.headers.get("x-openwebui-chat-id")
        user_id = request.headers.get("x-openwebui-user-id", "local")
        # Include all configuration: changed tools/model/system prompt start fresh.
        key = (
            digest([user_id, chat_id, body.model, profile.model_dump(mode="json")])
            if chat_id and not isolated
            else None
        )
        response_id = "chatcmpl-" + uuid.uuid4().hex
        created = int(time.time())

        if not body.stream:

            async def collect():
                rendered = []
                result = None
                async with (
                    asyncio.timeout(timeout),
                    aclosing(
                        backends[profile.backend].stream(profile, replay(messages), None)
                    ) as events,
                ):
                    async for event in events:
                        if isinstance(event, Text):
                            if result is not None:
                                raise RuntimeError("Backend emitted text after its result")
                            rendered.append(event.content)
                        elif isinstance(event, Done):
                            if result is not None:
                                raise RuntimeError("Backend emitted duplicate results")
                            result = event
                if result is None:
                    raise RuntimeError("Backend did not return a result")
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
                while not await request.is_disconnected():
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
                    error_payload("Isolated completion failed; retry the request", "server_error"),
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
            store = request.app.state.store
            row = store.get(key) if key else None
            resume = None
            if (
                row
                and row["cwd"] == str(profile.cwd)
                and row["prefix_hash"] == digest(messages[:-1])
            ):
                resume = row["session_id"]
            prompt = messages[-1]["content"] if resume else replay(messages)
            if key:
                # A resumed agent session mutates before the result arrives. Remove
                # the row now so crash/cancellation/failure forces a fresh replay.
                store.delete(key)
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
                    if isinstance(event, Text):
                        if result is not None:
                            raise RuntimeError("Backend emitted text after its result")
                        rendered.append(event.content)
                        yield chunk({"content": event.content})
                    elif isinstance(event, Done):
                        if result is not None:
                            raise RuntimeError("Backend emitted duplicate results")
                        result = event
                if result is None:
                    raise RuntimeError("Backend did not return a session")
                if key:
                    prefix = messages + [{"role": "assistant", "content": "".join(rendered)}]
                    store.save(key, result.session_id, str(profile.cwd), digest(prefix))
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
            except Exception:  # noqa: BLE001 - HTTP headers are already sent; report via SSE
                yield sse(
                    error_payload(
                        "Agent turn failed; retry to start a fresh session", "server_error"
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
