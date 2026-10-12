import asyncio
import json
import os
import threading
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx2
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from test_backends import mock_client, result

from openwebui_bridge.backends import ClaudeBackend, codex_command
from openwebui_bridge.config import OpenWebUIConfig, Profile, RemoteMCPConfig, load_settings
from openwebui_bridge.openwebui_mcp import (
    CONFIG_ENV,
    MCP_CONFIG_ENV,
    MCP_TOKENS_ENV,
    TOKEN_ENV,
    RemoteMCPClient,
    connection_env,
    create_server,
    server_command,
)

SCHEMA = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
GATEWAY_NAMES = ["litellm_github-search", "litellm_slack-search"]


def config(**kwargs):
    return OpenWebUIConfig(url="http://webui", **kwargs)


def upstreams():
    return {"litellm": RemoteMCPConfig(url="http://gateway/mcp", api_key_env="LITELLM_API_KEY")}


def protocol_handler(request):
    assert request.url.path == "/mcp"
    assert request.headers["authorization"] == "Bearer gateway-key"
    if request.method == "DELETE":
        return httpx2.Response(200)
    body = json.loads(request.content)
    if "id" not in body:
        return httpx2.Response(202)
    method = body["method"]
    if method == "initialize":
        result = {
            "protocolVersion": body["params"]["protocolVersion"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fixture", "version": "1"},
        }
    elif method == "tools/list":
        result = {
            "tools": [
                {
                    "name": "github-search",
                    "description": "Search issues",
                    "inputSchema": SCHEMA,
                    "annotations": {"readOnlyHint": True},
                },
                {"name": "slack-search", "description": "Search messages", "inputSchema": SCHEMA},
            ]
        }
    elif method == "tools/call":
        assert body["params"]["name"] == "slack-search"
        assert body["params"]["arguments"] == {"query": "hello"}
        result = {
            "content": [{"type": "text", "text": "found"}],
            "structuredContent": {"count": 1},
            "isError": False,
        }
    else:
        raise AssertionError(method)
    return httpx2.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})


async def test_individual_tools_and_routing():
    server = create_server(
        config(),
        "webui-key",
        mcp_servers=upstreams(),
        mcp_tokens={"litellm": "gateway-key"},
        mcp_transport=httpx2.MockTransport(protocol_handler),
    )
    tools = {tool.name: tool for tool in await server.list_tools()}
    assert "list_memories" in tools
    assert set(GATEWAY_NAMES) <= tools.keys()
    assert "list_litellm_tools" not in tools and "call_litellm_tool" not in tools
    assert tools[GATEWAY_NAMES[0]].input_schema == SCHEMA
    assert tools[GATEWAY_NAMES[0]].annotations.read_only_hint
    assert server._tool_input_schema(GATEWAY_NAMES[0]) == SCHEMA
    response = await server.call_tool(GATEWAY_NAMES[1], {"query": "hello"})
    assert response.content[0].text == "found"
    assert response.structured_content == {"count": 1}
    assert not response.is_error
    with pytest.raises(Exception, match="Unknown tool"):
        await server.call_tool("litellm_unknown", {})


@pytest.mark.parametrize(
    "prefix,expected",
    [
        (None, "litellm_slack-search"),
        ("tools_", "tools_slack-search"),
        ("", "slack-search"),
    ],
)
async def test_configurable_prefix(prefix, expected):
    integration = config()
    servers = upstreams()
    servers["litellm"] = RemoteMCPConfig(
        url="http://gateway/mcp",
        prefix=prefix,
    )
    server = create_server(
        integration,
        "webui-key",
        mcp_servers=servers,
        mcp_tokens={"litellm": "gateway-key"},
        mcp_transport=httpx2.MockTransport(protocol_handler),
    )
    tools = {tool.name: tool for tool in await server.list_tools()}
    assert expected in tools
    assert tools[expected].input_schema == SCHEMA
    response = await server.call_tool(expected, {"query": "hello"})
    assert response.content[0].text == "found"


async def test_duplicate_names_rejected():
    integration = config()
    servers = upstreams()
    servers["other"] = RemoteMCPConfig(url="http://gateway/mcp", prefix="litellm_")
    server = create_server(
        integration,
        "webui-key",
        mcp_servers=servers,
        mcp_tokens={"litellm": "gateway-key", "other": "gateway-key"},
        mcp_transport=httpx2.MockTransport(protocol_handler),
    )
    with pytest.raises(Exception, match="names conflict"):
        await server.list_tools()


@pytest.mark.parametrize("prefix", ["bad.prefix", "bad/prefix", "x" * 33])
def test_invalid_prefix(prefix):
    with pytest.raises(ValueError):
        RemoteMCPConfig(url="http://gateway/mcp", prefix=prefix)


@pytest.mark.parametrize("status", [403, 500, 302])
async def test_discovery_failures_sanitized(status):
    def handler(request):
        return httpx2.Response(
            status,
            json={"private": "upstream-secret"},
            headers={"Location": "http://other-host/mcp"},
        )

    client = RemoteMCPClient(upstreams()["litellm"], "private-key", httpx2.MockTransport(handler))
    with pytest.raises(Exception, match="Upstream MCP request failed") as exc:
        await client.discover()
    assert "private-key" not in str(exc.value)
    assert "upstream-secret" not in str(exc.value)


async def test_upstream_mcp_errors():
    def handler(request):
        body = json.loads(request.content)
        if body.get("method") == "tools/call":
            return httpx2.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "content": [{"type": "text", "text": "tool failed"}],
                        "isError": True,
                    },
                },
            )
        return protocol_handler(request)

    server = create_server(
        config(),
        "key",
        mcp_servers=upstreams(),
        mcp_tokens={"litellm": "gateway-key"},
        mcp_transport=httpx2.MockTransport(handler),
    )
    response = await server.call_tool(GATEWAY_NAMES[0], {"query": "hello"})
    assert response.is_error


def test_credentials_and_shared_config(tmp_path, monkeypatch):
    path = tmp_path / "profiles.toml"
    path.write_text("""
[mcp_servers.litellm]
url = "http://gateway"
api_key_env = "TEST_GATEWAY_KEY"
[profiles.claude]
cwd = "."
[profiles.off]
cwd = "."
openwebui = false
mcp_servers = {}
[profiles.override]
cwd = "."
[profiles.override.mcp_servers.other]
url = "http://other/mcp"
""")
    profiles = load_settings(path).profiles
    integration = profiles["claude"].openwebui
    servers = profiles["claude"].mcp_servers
    assert integration is None
    assert profiles["off"].mcp_servers == {}
    assert set(profiles["override"].mcp_servers) == {"other"}
    assert profiles["off"].openwebui is None
    monkeypatch.delenv("OPENWEBUI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="TEST_GATEWAY_KEY"):
        connection_env(integration, servers)
    monkeypatch.setenv("TEST_GATEWAY_KEY", "private-gateway-key")
    environment = connection_env(integration, servers)
    assert environment[TOKEN_ENV] == ""
    assert environment[CONFIG_ENV] == "null"
    assert json.loads(environment[MCP_TOKENS_ENV]) == {"litellm": "private-gateway-key"}
    assert "private-gateway-key" not in environment[CONFIG_ENV]
    monkeypatch.setenv("TEST_GATEWAY_KEY", "rotated")
    assert connection_env(integration, servers) != environment


@pytest.mark.parametrize("resume", [None, "session"])
@pytest.mark.parametrize("with_webui", [False, True])
async def test_backend_permissions_and_single_server(tmp_path, monkeypatch, resume, with_webui):
    monkeypatch.setenv("OPENWEBUI_API_KEY", "webui-key")
    monkeypatch.setenv("LITELLM_API_KEY", "gateway-key")

    async def discover(_self):
        from mcp.types import Tool

        return [
            Tool(name=name.removeprefix("litellm_"), input_schema=SCHEMA) for name in GATEWAY_NAMES
        ]

    monkeypatch.setattr(RemoteMCPClient, "discover", discover)
    client = mock_client(monkeypatch, [result()])
    profile = Profile(
        cwd=tmp_path, openwebui=config() if with_webui else None, mcp_servers=upstreams()
    )
    _ = [event async for event in ClaudeBackend().stream(profile, "hello", resume)]
    assert set(client.options.mcp_servers) == {"openwebui"}
    assert json.loads(client.options.env[MCP_TOKENS_ENV]) == {"litellm": "gateway-key"}
    hook = client.options.hooks["PreToolUse"][0].hooks[0]
    for name in GATEWAY_NAMES:
        assert f"mcp__openwebui__{name}" in client.options.allowed_tools
        assert await hook({"tool_name": f"mcp__openwebui__{name}"}, None, None) == {}
    denied = await hook({"tool_name": "mcp__openwebui__litellm_unknown"}, None, None)
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    command = codex_command(profile, resume, gateway_tools=GATEWAY_NAMES)
    assignment = next(value for value in command if value.startswith("mcp_servers.openwebui="))
    servers = tomllib.loads(assignment)["mcp_servers"]
    assert set(servers) == {"openwebui"}
    assert set(GATEWAY_NAMES) <= set(servers["openwebui"]["enabled_tools"])
    assert MCP_TOKENS_ENV in servers["openwebui"]["env_vars"]
    assert "gateway-key" not in " ".join(command)
    for name in GATEWAY_NAMES:
        assert f'mcp_servers.openwebui.tools.{name}.approval_mode="approve"' in command


async def test_stdio_discovery_and_execution():
    initializations = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            is_initialize = json.loads(body).get("method") == "initialize"
            if is_initialize:
                initializations.append(1)
            else:
                assert self.headers["Mcp-Session-Id"] == "fixture-session"
            response = protocol_handler(
                httpx2.Request(
                    "POST",
                    "http://fixture/mcp",
                    content=body,
                    headers={"Authorization": self.headers["Authorization"]},
                )
            )
            self.send_response(response.status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response.content)))
            self.send_header("Mcp-Session-Id", "fixture-session")
            self.end_headers()
            self.wfile.write(response.content)

        def do_GET(self):
            self.send_response(405)
            self.end_headers()

        def do_DELETE(self):
            self.send_response(200)
            self.end_headers()

    http_server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    thread.start()
    try:
        servers = {
            "litellm": RemoteMCPConfig(url=f"http://127.0.0.1:{http_server.server_port}/mcp")
        }
        params = StdioServerParameters(
            **server_command(),
            env={
                **os.environ,
                CONFIG_ENV: "null",
                TOKEN_ENV: "",
                MCP_CONFIG_ENV: json.dumps(
                    {name: value.model_dump() for name, value in servers.items()}
                ),
                MCP_TOKENS_ENV: json.dumps({"litellm": "gateway-key"}),
            },
        )
        async with (
            asyncio.timeout(15),
            stdio_client(params) as (reader, writer),
            ClientSession(reader, writer) as session,
        ):
            await session.initialize()
            tools = await session.list_tools()
            assert {tool.name for tool in tools.tools} == set(GATEWAY_NAMES)
            response = await session.call_tool(GATEWAY_NAMES[1], {"query": "hello"})
            assert not response.is_error and response.content[0].text == "found"
            assert len(initializations) == 1
    finally:
        http_server.shutdown()
        http_server.server_close()
        thread.join(timeout=2)
