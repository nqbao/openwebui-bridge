import asyncio
import base64
import json
from contextlib import asynccontextmanager

import httpx
import pytest

from openwebui_bridge.app import create_app
from openwebui_bridge.backends import Done, Text
from openwebui_bridge.config import Profile, Settings, load_settings
from openwebui_bridge.protocol import tool_line
from openwebui_bridge.store import Store


class FakeBackend:
    def __init__(self, content="answer"):
        self.content = content
        self.calls = []
        self.fail = False
        self.active = 0
        self.max_active = 0

    async def stream(self, profile, prompt, resume):
        self.calls.append((profile, prompt, resume))
        self.active += 1
        self.max_active = max(self.active, self.max_active)
        try:
            await asyncio.sleep(0.01)
            yield Text(self.content)
            if self.fail:
                raise RuntimeError("secret backend details")
            yield Done(f"session-{len(self.calls)}", {"input_tokens": 3, "output_tokens": 2})
        finally:
            self.active -= 1


@asynccontextmanager
async def client_for(tmp_path, backend=None, **kwargs):
    backend = backend or FakeBackend()
    profile = Profile(cwd=tmp_path)
    settings = Settings(
        profiles={"claude-code:test": profile, "claude-code:other": profile},
        database=tmp_path / "sessions.db",
        **kwargs,
    )
    app = create_app(settings, {"claude": backend})
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield client, backend, app


def msg(role, content):
    return {"role": role, "content": content}


def image_part(data=b"\x89PNG\r\n\x1a\nfixture", kind="png"):
    return {
        "type": "image_url",
        "image_url": {
            "url": f"data:image/{kind};base64," + base64.b64encode(data).decode(),
            "detail": "high",
        },
    }


async def test_image_input_resume_and_edit_replay(tmp_path):
    image = image_part()
    content = [{"type": "text", "text": "Describe this"}, image]
    async with client_for(tmp_path) as (client, backend, _):
        history = [msg("user", content)]
        response = await complete(client, history)
        assert response.status_code == 200
        prompt = backend.calls[-1][1]
        assert prompt[0] == {"type": "text", "text": "Describe this"}
        assert prompt[1]["type"] == "image"
        assert prompt[1]["source"]["data"] == image["image_url"]["url"].split(",")[1]
        history += [msg("assistant", "answer"), msg("user", "More details")]
        await complete(client, history)
        assert backend.calls[-1][1:] == ("More details", "session-1")
        history[0]["content"][1] = image_part(b"\x89PNG\r\n\x1a\nchanged")
        await complete(client, history)
        assert backend.calls[-1][2] is None
        replayed = backend.calls[-1][1]
        images = [block for block in replayed if block["type"] == "image"]
        assert len(images) == 1
        assert images[0]["source"]["data"] == base64.b64encode(b"\x89PNG\r\n\x1a\nchanged").decode()
        assert "More details" in replayed[-1]["text"]
        assert images[0]["source"]["data"] not in "".join(
            block.get("text", "") for block in replayed
        )


async def test_latest_image_on_resumed_turn_and_nonstreaming_task(tmp_path):
    async with client_for(tmp_path) as (client, backend, _):
        await complete(client, [msg("user", "first")])
        messages = [msg("user", "first"), msg("assistant", "answer"), msg("user", [image_part()])]
        await complete(client, messages)
        assert backend.calls[-1][2] == "session-1"
        assert backend.calls[-1][1][0]["type"] == "image"
        response = await complete(client, messages, stream=False)
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "answer"
        assert backend.calls[-1][2] is None
        assert any(block["type"] == "image" for block in backend.calls[-1][1])


@pytest.mark.parametrize(
    "part",
    [
        {"type": "image_url", "image_url": {"url": "https://example.com/image.png"}},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,!!!!"}},
        image_part(b"not an image"),
        image_part(kind="svg+xml"),
        image_part(b"\x89PNG\r\n\x1a\n" + b"x" * (5 * 1024 * 1024)),
    ],
)
async def test_invalid_images_fail_before_backend(tmp_path, part):
    async with client_for(tmp_path) as (client, backend, _):
        response = await complete(client, [msg("user", [part])])
        assert response.status_code == 400
        assert backend.calls == []
        assert part["image_url"]["url"] not in response.text


@pytest.mark.parametrize("role", ["system", "assistant", "developer"])
async def test_images_only_in_user_messages(tmp_path, role):
    async with client_for(tmp_path) as (client, backend, _):
        response = await complete(client, [msg(role, [image_part()]), msg("user", "hi")])
        assert response.status_code == 400
        assert backend.calls == []


@pytest.mark.parametrize("count,size", [(21, 10), (5, 5 * 1024 * 1024 - 8)])
async def test_image_request_limits(tmp_path, count, size):
    async with client_for(tmp_path) as (client, backend, _):
        response = await complete(
            client, [msg("user", [image_part(b"\x89PNG\r\n\x1a\n" + b"x" * size)] * count)]
        )
        assert response.status_code == 400
        assert backend.calls == []


async def test_codex_images_resume_and_replay(tmp_path):
    backend = FakeBackend()
    settings = Settings(
        profiles={"codex:test": Profile(cwd=tmp_path, backend="codex", allowed_tools=[])},
        database=tmp_path / "sessions.db",
    )
    app = create_app(settings, {"codex": backend})
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await complete(client, [msg("user", [image_part()])], model="codex:test")
        assert response.status_code == 200
        assert backend.calls[-1][1][0]["type"] == "image"
        history = [
            msg("user", [image_part()]),
            msg("assistant", "answer"),
            msg("user", [{"type": "text", "text": "Compare"}, image_part()]),
        ]
        await complete(client, history, model="codex:test")
        assert backend.calls[-1][2] == "session-1"
        assert len(backend.calls[-1][1]) == 2
        history[0]["content"] = [image_part(b"\x89PNG\r\n\x1a\nchanged")]
        await complete(client, history, model="codex:test")
        assert backend.calls[-1][2] is None
        assert sum(block["type"] == "image" for block in backend.calls[-1][1]) == 2


async def complete(
    client, messages, chat="chat-1", user="user-1", model="claude-code:test", **fields
):
    headers = {"X-OpenWebUI-User-Id": user}
    if chat is not None:
        headers["X-OpenWebUI-Chat-Id"] = chat
    return await client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": model, "stream": True, "messages": messages, **fields},
    )


def chunks(response):
    return [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]


async def test_models_auth_and_unknown_profile(tmp_path):
    async with client_for(tmp_path, api_key="test-key") as (client, backend, _):
        assert (await client.get("/v1/models")).status_code == 401
        response = await client.get("/v1/models", headers={"Authorization": "Bearer test-key"})
        assert response.json()["data"][0]["id"] == "claude-code:test"
        response = await complete(client, [msg("user", "hi")])
        assert response.status_code == 401
        assert backend.calls == []
    async with client_for(tmp_path) as (client, _, _):
        assert (await complete(client, [msg("user", "hi")], model="missing")).status_code == 404


async def test_stream_shape_usage_and_native_resume(tmp_path):
    async with client_for(tmp_path) as (client, backend, _):
        history = [msg("user", "first")]
        response = await complete(client, history, stream_options={"include_usage": True})
        events = chunks(response)
        assert events[0]["choices"][0]["delta"]["role"] == "assistant"
        assert events[1]["choices"][0]["delta"] == {"content": "answer"}
        assert events[2]["choices"][0]["finish_reason"] == "stop"
        assert events[3]["choices"] == []
        assert events[3]["usage"]["total_tokens"] == 5
        assert len({e["id"] for e in events}) == 1
        assert response.text.endswith("data: [DONE]\n\n")
        history += [msg("assistant", "answer"), msg("user", "second")]
        await complete(client, history)
        assert backend.calls[-1][1:] == ("second", "session-1")


async def test_tools_are_text_and_hash_includes_rendered_output(tmp_path):
    content = tool_line("Read", {"file_path": "src/main.py"}) + "answer"
    async with client_for(tmp_path, FakeBackend(content)) as (client, backend, _):
        await complete(client, [msg("user", "first")])
        response = await complete(
            client, [msg("user", "first"), msg("assistant", content), msg("user", "second")]
        )
        assert "tool_calls" not in response.text
        assert backend.calls[-1][2] == "session-1"


@pytest.mark.parametrize(
    "fields",
    [
        {"tools": [{"type": "function", "function": {"name": "search_web"}}]},
        {
            "tools": [{"type": "function", "function": {"name": "search_web"}}],
            "tool_choice": "auto",
        },
        {
            "tools": [{"type": "function", "function": {"name": "search_web"}}],
            "tool_choice": "required",
        },
        {
            "tools": [{"type": "function", "function": {"name": "search_web"}}],
            "tool_choice": {"type": "function", "function": {"name": "search_web"}},
        },
        {
            "functions": [{"name": "search_web", "parameters": {"type": "object"}}],
            "function_call": {"name": "search_web"},
        },
    ],
)
async def test_frontend_tool_metadata_is_accepted_without_affecting_agent(tmp_path, fields):
    async with client_for(tmp_path) as (client, backend, _):
        response = await complete(client, [msg("user", "first")], **fields)
        assert response.status_code == 200
        assert chunks(response)[-1]["choices"][0]["finish_reason"] == "stop"
        assert backend.calls[0][1:] == ("first", None)
        assert backend.calls[0][0].allowed_tools == ["Glob", "Grep", "Read"]
        assert "tool_calls" not in response.text
        assert "search_web" not in response.text
        # Frontend tool metadata does not become native session context or break
        # the prefix hash when the next request has a different set of schemas.
        response = await complete(
            client, [msg("user", "first"), msg("assistant", "answer"), msg("user", "second")]
        )
        assert response.status_code == 200
        assert backend.calls[-1][1:] == ("second", "session-1")


@pytest.mark.parametrize("changed", ["edit", "regenerate", "system"])
async def test_changed_prefix_starts_fresh(tmp_path, changed):
    async with client_for(tmp_path) as (client, backend, _):
        await complete(client, [msg("user", "first")])
        if changed == "regenerate":
            messages = [msg("user", "first")]
        else:
            messages = [
                msg("user", "edited" if changed == "edit" else "first"),
                msg("assistant", "answer"),
                msg("user", "next"),
            ]
            if changed == "system":
                messages.insert(0, msg("system", "new system prompt"))
        await complete(client, messages)
        assert backend.calls[-1][2] is None
        if changed != "regenerate":
            assert "transcript" in backend.calls[-1][1]


async def test_stateless_flattens_history_and_never_saves(tmp_path):
    async with client_for(tmp_path) as (client, backend, app):
        messages = [
            msg("system", "help"),
            msg("user", "first"),
            msg("assistant", "previous"),
            msg("user", "next"),
        ]
        await complete(client, messages, chat=None)
        assert "previous" in backend.calls[0][1]
        assert backend.calls[0][2] is None
        assert app.state.store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


async def test_user_and_profile_isolation(tmp_path):
    async with client_for(tmp_path) as (client, backend, _):
        await complete(client, [msg("user", "first")])
        messages = [msg("user", "first"), msg("assistant", "answer"), msg("user", "next")]
        await complete(client, messages, user="other")
        assert backend.calls[-1][2] is None
        await complete(client, messages, model="claude-code:other")
        assert backend.calls[-1][2] is None


async def test_failure_invalidates_mutated_session(tmp_path):
    async with client_for(tmp_path) as (client, backend, app):
        await complete(client, [msg("user", "first")])
        messages = [msg("user", "first"), msg("assistant", "answer"), msg("user", "next")]
        backend.fail = True
        response = await complete(client, messages)
        assert chunks(response)[-1]["error"]["type"] == "server_error"
        assert "secret" not in response.text
        assert app.state.store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        backend.fail = False
        await complete(client, messages)
        assert backend.calls[-1][2] is None


async def test_per_chat_lock_reads_state_after_preceding_turn(tmp_path):
    async with client_for(tmp_path) as (client, backend, _):
        await asyncio.gather(
            complete(client, [msg("user", "first")]),
            complete(
                client, [msg("user", "first"), msg("assistant", "answer"), msg("user", "next")]
            ),
        )
        assert backend.max_active == 1
        assert backend.calls[-1][1:] == ("next", "session-1")


async def test_mapping_survives_restart(tmp_path):
    async with client_for(tmp_path) as (client, _, _):
        await complete(client, [msg("user", "first")])
    async with client_for(tmp_path) as (client, backend, _):
        await complete(
            client, [msg("user", "first"), msg("assistant", "answer"), msg("user", "next")]
        )
        assert backend.calls[-1][1:] == ("next", "session-1")


async def test_timeout_closes_backend_without_saving(tmp_path):
    class SlowBackend:
        closed = False

        async def stream(self, *_args):
            try:
                yield Text("partial")
                await asyncio.Event().wait()
            finally:
                self.closed = True

    backend = SlowBackend()
    async with client_for(tmp_path, backend, turn_timeout_seconds=0.05) as (client, _, app):
        response = await complete(client, [msg("user", "first")])
        assert chunks(response)[-1]["error"]["type"] == "server_error"
        assert backend.closed
        assert app.state.store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


async def test_disconnect_cancels_backend_before_releasing_chat(tmp_path):
    class SlowBackend:
        closed = False

        async def stream(self, *_args):
            try:
                yield Text("partial")
                await asyncio.Event().wait()
            finally:
                self.closed = True

    backend = SlowBackend()
    async with client_for(tmp_path, backend) as (_, _, app):
        disconnect = asyncio.Event()
        sent_request = False

        async def receive():
            nonlocal sent_request
            if not sent_request:
                sent_request = True
                return {
                    "type": "http.request",
                    "body": json.dumps(
                        {
                            "model": "claude-code:test",
                            "stream": True,
                            "messages": [msg("user", "first")],
                        }
                    ).encode(),
                    "more_body": False,
                }
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(event):
            if b"partial" in event.get("body", b""):
                disconnect.set()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "method": "POST",
            "scheme": "http",
            "path": "/v1/chat/completions",
            "raw_path": b"/v1/chat/completions",
            "query_string": b"",
            "root_path": "",
            "http_version": "1.1",
            "headers": [(b"content-type", b"application/json"), (b"x-openwebui-chat-id", b"chat")],
            "client": ("127.0.0.1", 123),
            "server": ("127.0.0.1", 80),
        }
        await asyncio.wait_for(app(scope, receive, send), 3)
        assert backend.closed
        assert app.state.store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


@pytest.mark.parametrize(
    "fields",
    [
        {"n": 2},
        {"response_format": {"type": "json_object"}},
        {"messages": [msg("user", [{"type": "image_url", "image_url": {"url": "x"}}])]},
        {"messages": [msg("assistant", "prefill")]},
    ],
)
async def test_unsupported_requests_fail_before_agent_runs(tmp_path, fields):
    async with client_for(tmp_path) as (client, backend, _):
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "claude-code:test",
                "stream": True,
                "messages": [msg("user", "hi")],
                **fields,
            },
        )
        assert response.status_code == 400
        assert response.json()["error"]["type"] == "invalid_request_error"
        assert backend.calls == []


async def test_nonstreaming_completion_is_tool_free_and_preserves_session(tmp_path):
    async with client_for(tmp_path, api_key="private-api-key") as (client, backend, _):
        client.headers["Authorization"] = "Bearer private-api-key"
        await complete(client, [msg("user", "first")])
        response = await complete(client, [msg("user", "private-background-prompt")], stream=False)
        assert response.status_code == 200
        assert response.json()["object"] == "chat.completion"
        assert response.json()["choices"] == [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "answer"},
            }
        ]
        assert response.json()["usage"]["total_tokens"] == 5
        task_profile, task_prompt, task_resume = backend.calls[-1]
        assert task_profile.allowed_tools == []
        assert task_profile.model == "haiku"
        assert task_profile.system_prompt != backend.calls[0][0].system_prompt
        assert task_prompt == "private-background-prompt"
        assert task_resume is None
        assert len(backend.calls) == 2
        await complete(
            client, [msg("user", "first"), msg("assistant", "answer"), msg("user", "second")]
        )
        assert backend.calls[-1][1:] == ("second", "session-1")


@pytest.mark.parametrize("stream", [False, True])
async def test_explicit_background_task_uses_isolated_agent(tmp_path, stream):
    async with client_for(tmp_path) as (client, backend, app):
        await complete(client, [msg("user", "first")])
        before = [tuple(row) for row in app.state.store.db.execute("SELECT * FROM sessions")]
        client.headers["X-OpenWebUI-Task"] = "title_generation"
        response = await complete(client, [msg("user", "private-title-prompt")], stream=stream)
        assert response.status_code == 200
        assert backend.calls[-1][0].allowed_tools == []
        assert backend.calls[-1][2] is None
        after = [tuple(row) for row in app.state.store.db.execute("SELECT * FROM sessions")]
        assert after == before
        if stream:
            assert chunks(response)[-1]["choices"][0]["finish_reason"] == "stop"
        else:
            assert response.json()["choices"][0]["message"]["content"] == "answer"


async def test_stream_defaults_to_isolated_json_and_replays_history(tmp_path):
    async with client_for(tmp_path, task_model="configured-task-model") as (client, backend, app):
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "claude-code:test",
                "messages": [
                    msg("user", "first"),
                    msg("assistant", "earlier answer"),
                    msg("user", "summarize"),
                ],
            },
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
        assert backend.calls[-1][0].model == "configured-task-model"
        assert "earlier answer" in backend.calls[-1][1]
        assert backend.calls[-1][2] is None
        assert app.state.store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


async def test_failed_nonstreaming_request_returns_json_error_and_preserves_session(tmp_path):
    async with client_for(tmp_path) as (client, backend, app):
        await complete(client, [msg("user", "first")])
        before = [tuple(row) for row in app.state.store.db.execute("SELECT * FROM sessions")]
        backend.fail = True
        response = await complete(client, [msg("user", "background")], stream=False)
        assert response.status_code == 502
        assert response.json()["error"]["type"] == "server_error"
        assert "secret" not in response.text
        assert [
            tuple(row) for row in app.state.store.db.execute("SELECT * FROM sessions")
        ] == before


async def test_nonstreaming_timeout_closes_backend(tmp_path):
    class SlowBackend:
        closed = False

        async def stream(self, *_args):
            try:
                yield Text("partial")
                await asyncio.Event().wait()
            finally:
                self.closed = True

    backend = SlowBackend()
    async with client_for(tmp_path, backend, task_timeout_seconds=0.03) as (client, _, app):
        response = await complete(client, [msg("user", "background")], stream=False)
        assert response.status_code == 504
        assert response.json()["error"]["type"] == "server_error"
        assert backend.closed
        assert app.state.store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


async def test_rejection_logs_reason_without_prompt_or_api_key(tmp_path, caplog):
    async with client_for(tmp_path, api_key="private-api-key") as (client, backend, _):
        client.headers["Authorization"] = "Bearer private-api-key"
        response = await complete(client, [msg("user", "private-prompt")], n=2)
        assert response.status_code == 400
        assert "Only n=1 is supported" in caplog.text
        assert "private-prompt" not in caplog.text
        assert "private-api-key" not in caplog.text
        assert backend.calls == []


async def test_nonstreaming_disconnect_cancels_backend(tmp_path):
    disconnect = asyncio.Event()

    class SlowBackend:
        closed = False

        async def stream(self, *_args):
            try:
                disconnect.set()
                yield Text("partial")
                await asyncio.Event().wait()
            finally:
                self.closed = True

    backend = SlowBackend()
    async with client_for(tmp_path, backend) as (_, _, app):
        sent_request = False

        async def receive():
            nonlocal sent_request
            if not sent_request:
                sent_request = True
                return {
                    "type": "http.request",
                    "body": json.dumps(
                        {
                            "model": "claude-code:test",
                            "stream": False,
                            "messages": [msg("user", "background")],
                        }
                    ).encode(),
                    "more_body": False,
                }
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(_event):
            pass

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "method": "POST",
            "scheme": "http",
            "path": "/v1/chat/completions",
            "raw_path": b"/v1/chat/completions",
            "query_string": b"",
            "root_path": "",
            "http_version": "1.1",
            "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 123),
            "server": ("127.0.0.1", 80),
        }
        await asyncio.wait_for(app(scope, receive, send), 3)
        assert backend.closed
        assert app.state.store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


async def test_codex_chat_profile_routes_tasks_to_tool_free_claude(tmp_path):
    claude = FakeBackend()
    codex = FakeBackend()
    settings = Settings(
        profiles={
            "codex:test": Profile(
                cwd=tmp_path,
                backend="codex",
                allowed_tools=[],
                model="codex-model",
            )
        },
        database=tmp_path / "sessions.db",
    )
    app = create_app(settings, {"claude": claude, "codex": codex})
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await complete(
            client, [msg("user", "generate title")], model="codex:test", stream=False
        )
        assert response.status_code == 200
        assert response.json()["model"] == "codex:test"
        assert claude.calls[-1][0].model == "haiku"
        assert claude.calls[-1][0].allowed_tools == []
        assert codex.calls == []


def test_config_paths_and_read_only_enforcement(tmp_path):
    path = tmp_path / "profiles.toml"
    path.write_text('[profiles."claude-code:test"]\ncwd = "."\n')
    settings = load_settings(path)
    assert settings.profiles["claude-code:test"].cwd == tmp_path
    with pytest.raises(ValueError, match="Read, Glob, Grep"):
        Profile(cwd=tmp_path, allowed_tools=["Task"])
    coding = Profile(
        cwd=tmp_path, allowed_tools=["Read", "Edit", "Bash(git diff:*)", "WebFetch", "WebSearch"]
    )
    assert len(coding.allowed_tools) == 5
    with pytest.raises(ValueError):
        Profile(cwd=tmp_path, permission_mode="bypassPermissions")


def test_database_lease_blocks_second_process(tmp_path):
    store = Store(tmp_path / "sessions.db")
    try:
        with pytest.raises(RuntimeError, match="one worker"):
            Store(tmp_path / "sessions.db")
    finally:
        store.close()
