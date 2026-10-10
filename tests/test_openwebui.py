import asyncio
import json
import os
import tomllib

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from test_backends import mock_client, result

from openwebui_bridge.app import create_app
from openwebui_bridge.backends import ClaudeBackend, Done, Text, codex_command
from openwebui_bridge.config import OpenWebUIConfig, Profile, Settings, load_settings
from openwebui_bridge.openwebui_mcp import (
    CONFIG_ENV,
    TOKEN_ENV,
    connection_env,
    create_server,
    server_command,
)


def unpack(result):
    return json.loads(result.content[0].text)


async def test_memory_path_and_type_round_trip():
    calls = []
    memory = {"id": "one", "content": "old", "path": "old/path", "type": "context"}

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=[memory.copy()])
        payload = json.loads(request.content)
        calls.append(payload)
        memory.update(payload)
        return httpx.Response(200, json=memory.copy())

    server = create_server(
        OpenWebUIConfig(url="http://webui", memory_write=True), "key", httpx.MockTransport(handler)
    )
    added = unpack(
        await server.call_tool(
            "add_memory",
            {
                "content": "uses uv",
                "path": "projects/bridge",
                "type": "context",
            },
        )
    )
    assert added["path"] == "projects/bridge"
    assert added["type"] == "context"
    updated = unpack(
        await server.call_tool(
            "update_memory",
            {
                "memory_id": "one",
                "path": "preferences",
                "type": "user",
            },
        )
    )
    assert updated["content"] == "uses uv"
    assert calls[-1] == {"path": "preferences", "type": "user"}
    updated = unpack(
        await server.call_tool(
            "update_memory",
            {
                "memory_id": "one",
                "content": "prefers uv",
                "path": None,
                "type": None,
            },
        )
    )
    assert calls[-1] == {"content": "prefers uv"}
    assert updated["path"] == "preferences"
    assert updated["type"] == "user"
    cleared = unpack(await server.call_tool("update_memory", {"memory_id": "one", "path": ""}))
    assert calls[-1] == {"path": ""}
    assert cleared["path"] == ""
    listed = unpack(await server.call_tool("list_memories", {}))["items"][0]
    assert listed["path"] == "" and listed["type"] == "user"


async def test_memory_write_schema_and_invalid_updates():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={})

    server = create_server(
        OpenWebUIConfig(url="http://webui", memory_write=True), "key", httpx.MockTransport(handler)
    )
    tools = {tool.name: tool for tool in await server.list_tools()}
    assert tools["add_memory"].input_schema["required"] == ["content"]
    assert tools["update_memory"].input_schema["required"] == ["memory_id"]
    for name in ("add_memory", "update_memory"):
        assert {"path", "type"} <= tools[name].input_schema["properties"].keys()
    with pytest.raises(Exception, match="Provide content, path, or type"):
        await server.call_tool("update_memory", {"memory_id": "one"})
    with pytest.raises(Exception, match="user|context"):
        await server.call_tool("add_memory", {"content": "test", "type": "invalid"})
    assert calls == []


@pytest.mark.parametrize(
    "knowledge,memory,write",
    [
        (True, True, False),
        (True, True, True),
        (True, False, False),
        (False, True, False),
        (False, False, False),
    ],
)
async def test_only_configured_tools_exposed(knowledge, memory, write):
    config = OpenWebUIConfig(
        url="http://webui", knowledge=knowledge, memory=memory, memory_write=write
    )
    server = create_server(config, "key")
    tools = await server.list_tools()
    assert {tool.name for tool in tools} == set(config.tool_names())
    for tool in tools:
        assert tool.annotations.read_only_hint == (
            tool.name not in {"add_memory", "update_memory", "delete_memory"}
        )
    if not write:
        with pytest.raises(Exception, match="Unknown tool"):
            await server.call_tool("delete_memory", {"memory_id": "memory"})


async def test_knowledge_search_reads_and_access_checks():
    calls = []

    def handler(request):
        assert request.headers["authorization"] == "Bearer user-key"
        calls.append(
            (
                request.method,
                request.url.path,
                dict(request.url.params),
                json.loads(request.content) if request.content else None,
            )
        )
        path = request.url.path
        if path == "/api/v1/knowledge/":
            return httpx.Response(
                200,
                json={"items": [{"id": "kb", "name": "Manual"}, {"id": "excluded"}], "total": 2},
            )
        if path == "/api/v1/knowledge/kb":
            return httpx.Response(200, json={"id": "kb", "files": None})
        if path == "/api/v1/knowledge/kb/files":
            if request.url.params.get("query"):
                return httpx.Response(200, json={"items": [], "total": 0})
            if request.url.params.get("page") == "1":
                return httpx.Response(200, json={"items": [{"id": "other"}], "total": 2})
            return httpx.Response(
                200, json={"items": [{"id": "file", "filename": "manual.txt"}], "total": 2}
            )
        if path == "/api/v1/retrieval/query/collection":
            return httpx.Response(
                200, json={"documents": [["the answer"]], "metadatas": [[{"file_id": "file"}]]}
            )
        if path == "/api/v1/files/file/data/content":
            return httpx.Response(200, json={"content": "abcdef"})
        raise AssertionError(path)

    server = create_server(
        OpenWebUIConfig(url="http://webui", knowledge_ids=["kb"]),
        "user-key",
        httpx.MockTransport(handler),
    )
    listing = unpack(await server.call_tool("list_knowledge", {}))
    assert [item["id"] for item in listing["items"]] == ["kb"]
    search = unpack(
        await server.call_tool(
            "search_knowledge", {"knowledge_id": "kb", "query": "question", "limit": 3}
        )
    )
    assert search["results"]["documents"] == [["the answer"]]
    assert calls[-1][-1] == {"collection_names": ["kb"], "query": "question", "k": 3}
    assert calls[-2][1] == "/api/v1/knowledge/kb"
    read = unpack(
        await server.call_tool(
            "read_knowledge_file",
            {"knowledge_id": "kb", "file_id": "file", "offset": 1, "max_chars": 2},
        )
    )
    assert read["content"] == "bc" and read["next_offset"] == 3
    assert read["filename"] == "manual.txt"
    count = len(calls)
    with pytest.raises(Exception, match="not enabled"):
        await server.call_tool("search_knowledge", {"knowledge_id": "excluded", "query": "x"})
    with pytest.raises(Exception, match="Invalid resource ID"):
        await server.call_tool("search_knowledge", {"knowledge_id": "../files", "query": "x"})
    assert len(calls) == count
    with pytest.raises(Exception, match="does not belong"):
        await server.call_tool(
            "read_knowledge_file", {"knowledge_id": "kb", "file_id": "unrelated"}
        )
    assert not any("files/unrelated" in call[1] for call in calls)


async def test_legacy_knowledge_file_list():
    def handler(request):
        if request.url.path.endswith("/data/content"):
            return httpx.Response(200, json={"content": "legacy text"})
        return httpx.Response(
            200, json={"id": "kb", "files": [{"id": "file", "filename": "old.txt"}]}
        )

    server = create_server(OpenWebUIConfig(url="http://webui"), "key", httpx.MockTransport(handler))
    assert (
        unpack(await server.call_tool("list_knowledge_files", {"knowledge_id": "kb"}))["total"] == 1
    )
    assert (
        unpack(
            await server.call_tool("read_knowledge_file", {"knowledge_id": "kb", "file_id": "file"})
        )["content"]
        == "legacy text"
    )


async def test_memory_read_search_and_opt_in_writes():
    calls = []

    def handler(request):
        calls.append(
            (
                request.method,
                request.url.path,
                json.loads(request.content) if request.content else None,
            )
        )
        if request.method == "GET":
            return httpx.Response(200, json=[{"id": "one", "content": "preference"}])
        if request.method == "DELETE":
            return httpx.Response(200, json=True)
        if request.url.path.endswith("query"):
            return httpx.Response(200, json={"documents": [["preference"]]})
        return httpx.Response(200, json={"id": "one", **json.loads(request.content)})

    server = create_server(
        OpenWebUIConfig(url="http://webui", memory_write=True), "key", httpx.MockTransport(handler)
    )
    assert unpack(await server.call_tool("list_memories", {}))["items"][0]["id"] == "one"
    assert unpack(await server.call_tool("search_memories", {"query": "preference"}))["results"][
        "documents"
    ] == [["preference"]]
    await server.call_tool("add_memory", {"content": "remember"})
    await server.call_tool("update_memory", {"memory_id": "one", "content": "corrected"})
    assert unpack(await server.call_tool("delete_memory", {"memory_id": "one"}))["deleted"] is True
    assert calls[-3:] == [
        ("POST", "/api/v1/memories/add", {"content": "remember"}),
        ("POST", "/api/v1/memories/one/update", {"content": "corrected"}),
        ("DELETE", "/api/v1/memories/one", None),
    ]


@pytest.mark.parametrize("status", [401, 403, 404, 500, 302])
async def test_api_errors_do_not_expose_credentials_or_bodies(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status, text="private-upstream-body", headers={"Location": "http://other-host"}
        )

    server = create_server(
        OpenWebUIConfig(url="http://webui"), "private-key", httpx.MockTransport(handler)
    )
    with pytest.raises(Exception) as exc:
        await server.call_tool("list_memories", {})
    assert f"HTTP {status}" in str(exc.value)
    assert "private" not in str(exc.value)
    assert len(calls) == 1


async def test_api_timeout_and_invalid_json_sanitized():
    def timeout(request):
        raise httpx.ReadTimeout("private-key", request=request)

    for handler, message in [
        (timeout, "Cannot reach"),
        (lambda _: httpx.Response(200, text="private-body"), "invalid JSON"),
    ]:
        server = create_server(
            OpenWebUIConfig(url="http://webui"), "private-key", httpx.MockTransport(handler)
        )
        with pytest.raises(Exception, match=message) as exc:
            await server.call_tool("list_memories", {})
        assert "private" not in str(exc.value)


def test_profile_configuration_and_codex_mcp_flags(tmp_path, monkeypatch):
    path = tmp_path / "profiles.toml"
    path.write_text("""[profiles."codex:test"]
backend = "codex"
cwd = "."
allowed_tools = []
[profiles."codex:test".openwebui]
url = "http://localhost:3000"
api_key_env = "TEST_WEBUI_KEY"
knowledge_ids = ["kb"]
memory_write = true
memory_path = "projects/test"
""")
    profile = load_settings(path).profiles["codex:test"]
    monkeypatch.setenv("TEST_WEBUI_KEY", "private-key")
    environment = connection_env(profile.openwebui)
    assert environment[TOKEN_ENV] == "private-key"
    assert json.loads(environment[CONFIG_ENV])["memory_write"] is True
    assert json.loads(environment[CONFIG_ENV])["memory_path"] == "projects/test"
    command = codex_command(profile, "session")
    mcp = next(value for value in command if value.startswith("mcp_servers.openwebui="))
    config = tomllib.loads(mcp)["mcp_servers"]["openwebui"]
    assert config["required"] is True
    assert config["env_vars"] == [CONFIG_ENV, TOKEN_ENV]
    assert set(config["enabled_tools"]) == set(profile.openwebui.tool_names())
    assert command.index(mcp) < command.index("resume")
    assert "private-key" not in " ".join(command)
    for tool in ("add_memory", "update_memory", "delete_memory"):
        assert f'mcp_servers.openwebui.tools.{tool}.approval_mode="approve"' in command
    read_profile = profile.model_copy(
        update={"openwebui": profile.openwebui.model_copy(update={"memory_write": False})}
    )
    assert not any("approval_mode" in argument for argument in codex_command(read_profile, None))
    monkeypatch.delenv("TEST_WEBUI_KEY")
    with pytest.raises(ValueError, match="TEST_WEBUI_KEY"):
        connection_env(profile.openwebui)


@pytest.mark.parametrize("resume", [None, "session"])
@pytest.mark.parametrize("notes_write", [False, True])
async def test_claude_mcp_tools_and_permission_hook(tmp_path, monkeypatch, resume, notes_write):
    monkeypatch.setenv("OPENWEBUI_API_KEY", "private-key")
    client = mock_client(monkeypatch, [result()])
    profile = Profile(
        cwd=tmp_path,
        allowed_tools=[],
        openwebui=OpenWebUIConfig(
            url="http://webui", notes=True, notes_write=notes_write, memory_path="projects/test"
        ),
    )
    _ = [event async for event in ClaudeBackend().stream(profile, "question", resume)]
    options = client.options
    assert options.resume == resume
    assert options.env[TOKEN_ENV] == "private-key"
    assert json.loads(options.env[CONFIG_ENV])["memory_path"] == "projects/test"
    assert "private-key" not in json.dumps(options.mcp_servers)
    assert options.mcp_servers["openwebui"]["args"] == server_command()["args"]
    assert options.allowed_tools == [
        f"mcp__openwebui__{name}" for name in profile.openwebui.tool_names()
    ]
    hook = options.hooks["PreToolUse"][0].hooks[0]
    assert await hook({"tool_name": "mcp__openwebui__search_knowledge"}, None, None) == {}
    assert await hook({"tool_name": "mcp__openwebui__read_note"}, None, None) == {}
    assert await hook({"tool_name": "mcp__openwebui__read_memory_path"}, None, None) == {}
    decision = await hook({"tool_name": "mcp__openwebui__create_note"}, None, None)
    if notes_write:
        assert decision == {}
    else:
        assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    for name in ["mcp__openwebui__delete_memory", "mcp__other__search_knowledge", "Bash"]:
        assert (await hook({"tool_name": name}, None, None))["hookSpecificOutput"][
            "permissionDecision"
        ] == "deny"


async def test_real_stdio_mcp_discovery():
    config = OpenWebUIConfig(
        url="http://127.0.0.1:9", memory=False, notes=True, notes_write=True, chat_history=True
    )
    params = StdioServerParameters(
        **server_command(),
        env={**os.environ, CONFIG_ENV: config.model_dump_json(), TOKEN_ENV: "test-key"},
    )
    async with (
        asyncio.timeout(15),
        stdio_client(params) as (reader, writer),
        ClientSession(reader, writer) as session,
    ):
        await session.initialize()
        tools = await session.list_tools()
        assert {tool.name for tool in tools.tools} == set(config.tool_names())
        response = await session.call_tool(
            "search_knowledge", {"knowledge_id": "../bad", "query": "x"}
        )
        assert response.is_error


async def test_empty_memories_search_is_empty_not_an_error():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=[])

    server = create_server(OpenWebUIConfig(url="http://webui"), "key", httpx.MockTransport(handler))
    assert unpack(await server.call_tool("search_memories", {"query": "anything"})) == {
        "results": []
    }
    assert calls == ["/api/v1/memories/"]


async def test_api_identity_changes_and_background_tasks(tmp_path, monkeypatch):
    class Backend:
        def __init__(self):
            self.calls = []

        async def stream(self, profile, prompt, resume):
            self.calls.append((profile, prompt, resume))
            yield Text("answer")
            yield Done(f"session-{len(self.calls)}")

    backend = Backend()
    config = OpenWebUIConfig(url="http://webui")
    profile = Profile(cwd=tmp_path, openwebui=config)
    app = create_app(
        Settings(profiles={"agent": profile}, database=tmp_path / "sessions.db"),
        {"claude": backend},
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):

        async def request(messages, **extra):
            return await client.post(
                "/v1/chat/completions",
                headers={"X-OpenWebUI-Chat-Id": "chat"},
                json={"model": "agent", "stream": True, "messages": messages, **extra},
            )

        history = [{"role": "user", "content": "first"}]
        monkeypatch.delenv("OPENWEBUI_API_KEY", raising=False)
        assert (await request(history)).status_code == 503
        assert not backend.calls
        monkeypatch.setenv("OPENWEBUI_API_KEY", "first-identity-key")
        assert (await request(history)).status_code == 200
        history += [
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "second"},
        ]
        await request(history)
        assert backend.calls[-1][2] == "session-1"
        monkeypatch.setenv("OPENWEBUI_API_KEY", "other-identity-key")
        await request(history)
        assert backend.calls[-1][2] is None
        await request(history, stream=False)
        assert backend.calls[-1][0].openwebui is None
        assert backend.calls[-1][0].allowed_tools == []
        monkeypatch.delenv("OPENWEBUI_API_KEY")
        assert (await request(history, stream=False)).status_code == 200
        assert b"identity-key" not in (tmp_path / "sessions.db").read_bytes()


@pytest.mark.parametrize(
    "values",
    [
        {"url": "file:///tmp/key"},
        {"url": "http://user:password@webui"},
        {"url": "http://webui?token=x"},
        {"url": "http://webui", "memory": False, "memory_write": True},
    ],
)
def test_invalid_connection_configuration(values):
    with pytest.raises(ValueError):
        OpenWebUIConfig(**values)
