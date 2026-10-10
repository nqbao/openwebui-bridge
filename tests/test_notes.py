import json
import tomllib

import httpx
import pytest
from test_openwebui import unpack

from openwebui_bridge.backends import codex_command
from openwebui_bridge.config import OpenWebUIConfig, load_settings
from openwebui_bridge.openwebui_mcp import create_server


@pytest.mark.parametrize("enabled,write", [(False, False), (True, False), (True, True)])
async def test_notes_tool_discovery(enabled, write):
    config = OpenWebUIConfig(
        url="http://webui", knowledge=False, memory=False, notes=enabled, notes_write=write
    )
    tools = {tool.name: tool for tool in await create_server(config, "key").list_tools()}
    assert set(tools) == set(config.tool_names())
    expected = {"list_notes", "search_notes", "read_note"} if enabled else set()
    if write:
        expected |= {"create_note", "update_note", "delete_note"}
        assert tools["delete_note"].annotations.destructive_hint is True
    assert set(tools) == expected


async def test_notes_read_search_and_slicing():
    calls = []
    note = {
        "id": "one",
        "title": "Test",
        "data": {"content": {"md": "abcdef"}},
        "write_access": True,
        "user": {"email": "private"},
        "meta": {"internal": True},
    }

    def handler(request):
        assert request.headers["authorization"] == "Bearer key"
        calls.append((request.url.path, dict(request.url.params)))
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"items": [note], "total": 1})
        if request.url.path.endswith("/notes/"):
            return httpx.Response(200, json=[note])
        return httpx.Response(200, json=note)

    server = create_server(
        OpenWebUIConfig(url="http://webui", notes=True), "key", httpx.MockTransport(handler)
    )
    listed = unpack(await server.call_tool("list_notes", {"page": 2}))
    assert listed["items"][0]["preview"] == "abcdef"
    assert "user" not in listed["items"][0] and "meta" not in listed["items"][0]
    searched = unpack(await server.call_tool("search_notes", {"query": "a & b", "page": 3}))
    assert searched["total"] == 1
    read = unpack(await server.call_tool("read_note", {"note_id": "one", "max_chars": 2}))
    assert read["content"] == "ab" and read["next_offset"] == 2
    read = unpack(await server.call_tool("read_note", {"note_id": "one", "offset": 2}))
    assert read["content"] == "cdef" and read["next_offset"] is None
    assert read["write_access"] is True
    assert calls[:2] == [
        ("/api/v1/notes/", {"page": "2"}),
        ("/api/v1/notes/search", {"query": "a & b", "page": "3"}),
    ]


async def test_notes_writes_preserve_metadata_and_omitted_fields():
    calls = []
    note = {
        "id": "one",
        "title": "Original",
        "write_access": True,
        "data": {"content": {"md": "old", "html": "retained"}, "other": 42},
        "meta": {"tag": "keep"},
        "access_grants": [{"permission": "read"}],
    }

    def handler(request):
        payload = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, payload))
        if request.method == "DELETE":
            return httpx.Response(200, json=True)
        if payload:
            note.update(payload)
        return httpx.Response(200, json=note.copy())

    server = create_server(
        OpenWebUIConfig(url="http://webui", notes=True, notes_write=True),
        "key",
        httpx.MockTransport(handler),
    )
    await server.call_tool("create_note", {"title": "New", "content": "# Markdown"})
    assert calls[-1] == (
        "POST",
        "/api/v1/notes/create",
        {"title": "New", "data": {"content": {"md": "# Markdown"}}},
    )
    note.update(title="Original", data={"content": {"md": "old", "html": "retained"}, "other": 42})
    await server.call_tool("update_note", {"note_id": "one", "content": "new"})
    assert calls[-1][2] == {
        "title": "Original",
        "data": {"content": {"md": "new", "html": "retained"}, "other": 42},
    }
    await server.call_tool("update_note", {"note_id": "one", "title": "Renamed"})
    assert calls[-1][2] == {"title": "Renamed"}
    await server.call_tool("update_note", {"note_id": "one", "content": ""})
    assert calls[-1][2]["data"]["content"]["md"] == ""
    assert note["meta"] == {"tag": "keep"}
    assert note["access_grants"] == [{"permission": "read"}]
    assert unpack(await server.call_tool("delete_note", {"note_id": "one"})) == {"deleted": True}
    assert calls[-1] == ("DELETE", "/api/v1/notes/one/delete", None)


async def test_notes_invalid_requests_and_write_access():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"id": "one", "title": "T", "write_access": False})

    server = create_server(
        OpenWebUIConfig(url="http://webui", notes=True, notes_write=True),
        "key",
        httpx.MockTransport(handler),
    )
    for tool in ("read_note", "update_note", "delete_note"):
        with pytest.raises(Exception, match="Invalid resource ID"):
            await server.call_tool(
                tool,
                {"note_id": "../escape", "content": "x"}
                if tool == "update_note"
                else {"note_id": "../escape"},
            )
    with pytest.raises(Exception, match="Provide title or content"):
        await server.call_tool("update_note", {"note_id": "one"})
    assert not calls
    with pytest.raises(Exception, match="write access"):
        await server.call_tool("update_note", {"note_id": "one", "content": "x"})
    assert len(calls) == 1 and calls[0].method == "GET"


@pytest.mark.parametrize("status", [401, 403, 404])
async def test_notes_api_errors(status):
    server = create_server(
        OpenWebUIConfig(url="http://webui", notes=True),
        "key",
        httpx.MockTransport(lambda _: httpx.Response(status, text="private")),
    )
    with pytest.raises(Exception, match=f"HTTP {status}") as exc:
        await server.call_tool("read_note", {"note_id": "one"})
    assert "private" not in str(exc.value)


def test_notes_shared_profile_config_and_codex_approval(tmp_path):
    path = tmp_path / "profiles.toml"
    path.write_text("""
[openwebui]
url = "http://webui"
notes = true
notes_write = true
[profiles.claude]
cwd = "."
[profiles.codex]
backend = "codex"
cwd = "."
allowed_tools = []
[profiles.off]
cwd = "."
[profiles.off.openwebui]
notes = false
notes_write = false
""")
    profiles = load_settings(path).profiles
    assert profiles["claude"].openwebui.notes_write
    assert not profiles["off"].openwebui.notes
    command = codex_command(profiles["codex"], None)
    mcp = tomllib.loads(next(arg for arg in command if arg.startswith("mcp_servers.openwebui=")))
    assert "create_note" in mcp["mcp_servers"]["openwebui"]["enabled_tools"]
    for name in ("create_note", "update_note", "delete_note"):
        assert f'mcp_servers.openwebui.tools.{name}.approval_mode="approve"' in command
    with pytest.raises(ValueError, match="notes_write requires"):
        OpenWebUIConfig(url="http://webui", notes_write=True)
