import json
import tomllib

import httpx
import pytest
from test_backends import mock_client, result
from test_openwebui import unpack

from openwebui_bridge.backends import ClaudeBackend, codex_command
from openwebui_bridge.config import OpenWebUIConfig, Profile, load_settings
from openwebui_bridge.openwebui_mcp import create_server


async def test_chat_search_and_selected_branch_slicing():
    calls = []
    history = {
        "currentId": "selected",
        "messages": {
            "root": {"role": "user", "content": "deployment?", "parentId": None},
            "discarded": {"role": "assistant", "content": "old branch", "parentId": "root"},
            "selected": {
                "role": "assistant",
                "parentId": "root",
                "content": [
                    {"type": "text", "text": "Use uv"},
                    {"type": "image_url", "image_url": {"url": "private-image"}},
                ],
                "tool_calls": [{"secret": "private-tool"}],
                "files": [{"secret": "private-file"}],
            },
        },
    }

    def handler(request):
        assert request.method == "GET"
        assert request.headers["authorization"] == "Bearer key"
        calls.append((request.url.path, dict(request.url.params)))
        if request.url.path.endswith("/search"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "one",
                        "title": "Deploy",
                        "snippet": "deployment",
                        "private": "hidden",
                    }
                ],
            )
        return httpx.Response(
            200,
            json={
                "id": "one",
                "title": "Deploy",
                "user_id": "private-user",
                "chat": {"history": history, "messages": [{"content": "stale flat list"}]},
            },
        )

    server = create_server(
        OpenWebUIConfig(url="http://webui", chat_history=True), "key", httpx.MockTransport(handler)
    )
    found = unpack(await server.call_tool("search_chats", {"query": "deploy & uv", "page": 2}))
    assert found["items"][0]["id"] == "one"
    assert "private" not in found["items"][0]
    assert calls[0] == ("/api/v1/chats/search", {"text": "deploy & uv", "page": "2"})
    first = unpack(await server.call_tool("read_chat", {"chat_id": "one", "max_chars": 10}))
    rest = unpack(await server.call_tool("read_chat", {"chat_id": "one", "offset": 10}))
    assert first["content"] + rest["content"] == "[user]\ndeployment?\n\n[assistant]\nUse uv"
    assert first["next_offset"] == 10 and rest["next_offset"] is None
    assert first["message_count"] == 2
    assert "private" not in json.dumps(rest)


@pytest.mark.parametrize("messages", [[], [{"role": "user", "content": "legacy"}]])
async def test_chat_flat_history_fallback(messages):
    server = create_server(
        OpenWebUIConfig(url="http://webui", chat_history=True),
        "key",
        httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "id": "one",
                    "chat": {"title": "Legacy", "messages": messages},
                },
            )
        ),
    )
    read = unpack(await server.call_tool("read_chat", {"chat_id": "one"}))
    assert read["message_count"] == len(messages)
    assert read["title"] == "Legacy"
    assert read["content"] == ("[user]\nlegacy" if messages else "")


@pytest.mark.parametrize(
    "history",
    [
        {"currentId": "missing", "messages": {}},
        {"currentId": "one", "messages": {"one": {"parentId": "one"}}},
        {"messages": {"one": {"content": "unselected"}}},
    ],
)
async def test_broken_chat_history_is_not_silently_mixed(history):
    server = create_server(
        OpenWebUIConfig(url="http://webui", chat_history=True),
        "key",
        httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "chat": {"history": history},
                },
            )
        ),
    )
    with pytest.raises(Exception, match="Chat history"):
        await server.call_tool("read_chat", {"chat_id": "one"})


async def test_chat_tools_reject_path_escape_and_mutating_tag_query():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=[])

    server = create_server(
        OpenWebUIConfig(url="http://webui", chat_history=True), "key", httpx.MockTransport(handler)
    )
    with pytest.raises(Exception, match="Invalid resource ID"):
        await server.call_tool("read_chat", {"chat_id": "../all/db"})
    with pytest.raises(Exception, match="Standalone tag"):
        await server.call_tool("search_chats", {"query": "tag:unused"})
    assert not calls
    assert unpack(await server.call_tool("search_chats", {"query": "not found"}))["items"] == []


@pytest.mark.parametrize("status", [401, 403, 404])
async def test_chat_access_errors(status):
    server = create_server(
        OpenWebUIConfig(url="http://webui", chat_history=True),
        "key",
        httpx.MockTransport(lambda _: httpx.Response(status, text="private")),
    )
    with pytest.raises(Exception, match=f"HTTP {status}") as exc:
        await server.call_tool("read_chat", {"chat_id": "one"})
    assert "private" not in str(exc.value)


async def test_shared_chat_config_and_both_backend_permissions(tmp_path, monkeypatch):
    path = tmp_path / "profiles.toml"
    path.write_text("""
[openwebui]
url = "http://webui"
knowledge = false
memory = false
chat_history = true
[profiles.claude]
cwd = "."
[profiles.codex]
backend = "codex"
cwd = "."
allowed_tools = []
[profiles.off]
cwd = "."
[profiles.off.openwebui]
chat_history = false
""")
    profiles = load_settings(path).profiles
    assert not profiles["off"].openwebui.tool_names()
    assert OpenWebUIConfig(url="http://webui").chat_history is False
    enabled = {"search_chats", "read_chat"}
    server = create_server(profiles["claude"].openwebui, "key")
    tools = await server.list_tools()
    assert {tool.name for tool in tools} == enabled
    assert all(tool.annotations.read_only_hint for tool in tools)
    command = codex_command(profiles["codex"], None)
    mcp = tomllib.loads(next(arg for arg in command if arg.startswith("mcp_servers.openwebui=")))
    assert set(mcp["mcp_servers"]["openwebui"]["enabled_tools"]) == enabled
    assert not any("approval_mode" in arg for arg in command)
    monkeypatch.setenv("OPENWEBUI_API_KEY", "test-key")
    client = mock_client(monkeypatch, [result()])
    profile = Profile(cwd=tmp_path, allowed_tools=[], openwebui=profiles["claude"].openwebui)
    _ = [event async for event in ClaudeBackend().stream(profile, "question", None)]
    assert set(client.options.allowed_tools) == {f"mcp__openwebui__{name}" for name in enabled}
    hook = client.options.hooks["PreToolUse"][0].hooks[0]
    assert await hook({"tool_name": "mcp__openwebui__read_chat"}, None, None) == {}
    assert (await hook({"tool_name": "mcp__openwebui__delete_chat"}, None, None))[
        "hookSpecificOutput"
    ]["permissionDecision"] == "deny"
