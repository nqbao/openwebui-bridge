import json

import httpx
import pytest
from test_openwebui import unpack

from openwebui_bridge.config import OpenWebUIConfig, load_settings
from openwebui_bridge.openwebui_mcp import create_server

MEMORIES = [
    {"id": "other", "path": "projects/other", "type": "context"},
    {"id": "prefix", "path": "projects/app-extra", "type": "context"},
    {"id": "project", "path": "projects/app", "type": "context"},
    {"id": "child", "path": "projects/app/build", "type": "context"},
    {"id": "shared", "path": None, "type": "user"},
    {"id": "unscoped", "path": None, "type": "context"},
    {"id": "other-user", "path": "projects/other", "type": "user"},
]


def fixture_server(**overrides):
    calls = []

    def handler(request):
        body = json.loads(request.content) if request.content else None
        calls.append((request.url.path, body))
        if request.method == "GET":
            return httpx.Response(200, json=MEMORIES)
        if request.url.path.endswith("/query"):
            rows = MEMORIES[: body["k"]]
            return httpx.Response(
                200,
                json={
                    "ids": [[row["id"] for row in rows]],
                    "documents": [[row["id"] + " text" for row in rows]],
                    "metadatas": [[{"path": row["path"]} for row in rows]],
                    "distances": [[1 - i / 10 for i in range(len(rows))]],
                },
            )
        return httpx.Response(200, json=body)

    config = OpenWebUIConfig(
        url="http://webui", memory_path="/projects//app/", memory_write=True, **overrides
    )
    return create_server(config, "key", httpx.MockTransport(handler)), calls


async def test_scoped_semantic_search_filters_before_limit_and_keeps_alignment():
    server, calls = fixture_server()
    result = unpack(await server.call_tool("search_memories", {"query": "build", "limit": 3}))
    assert calls[-1][1] == {"content": "build", "k": len(MEMORIES)}
    assert result["results"] == {
        "ids": [["project", "child", "shared"]],
        "documents": [["project text", "child text", "shared text"]],
        "metadatas": [[{"path": "projects/app"}, {"path": "projects/app/build"}, {"path": None}]],
        "distances": [[0.8, 0.7, 0.6]],
    }


async def test_scope_overrides_shared_toggle_and_all_account_escape():
    server, calls = fixture_server()
    result = unpack(await server.call_tool("list_memories", {"include_shared": False}))
    assert [row["id"] for row in result["items"]] == ["project", "child"]
    result = unpack(
        await server.call_tool(
            "list_memories", {"path": "/projects/other/", "include_shared": False}
        )
    )
    assert [row["id"] for row in result["items"]] == ["other", "other-user"]
    result = unpack(
        await server.call_tool("search_memories", {"query": "x", "path": "", "limit": 1})
    )
    assert result["results"]["ids"] == [["other"]]
    assert calls[-1][1]["k"] == 1
    result = unpack(await server.call_tool("list_memories", {"offset": 1, "limit": 1}))
    assert result["total"] == 3
    assert result["items"][0]["id"] == "child"


async def test_shared_default_can_be_disabled_and_overridden():
    server, _ = fixture_server(memory_include_shared=False)
    result = unpack(await server.call_tool("list_memories", {}))
    assert result["total"] == 2
    result = unpack(await server.call_tool("list_memories", {"include_shared": True}))
    assert result["total"] == 3


async def test_empty_scope_skips_vector_query():
    server, calls = fixture_server()
    result = unpack(
        await server.call_tool(
            "search_memories",
            {
                "query": "x",
                "path": "missing",
                "include_shared": False,
            },
        )
    )
    assert result == {"results": []}
    assert len(calls) == 1


async def test_default_write_path_explicit_shared_and_update_preserves_path():
    server, calls = fixture_server()
    await server.call_tool("add_memory", {"content": "project fact"})
    assert calls[-1][1] == {"content": "project fact", "path": "projects/app"}
    await server.call_tool("add_memory", {"content": "preference", "path": "", "type": "user"})
    assert calls[-1][1] == {"content": "preference", "path": "", "type": "user"}
    await server.call_tool("add_memory", {"content": "fact", "path": "/projects/other/"})
    assert calls[-1][1]["path"] == "projects/other"
    await server.call_tool("update_memory", {"memory_id": "project", "content": "updated"})
    assert calls[-1][1] == {"content": "updated"}


async def test_path_browsing_uses_upstream_contract():
    server, calls = fixture_server()
    await server.call_tool(
        "list_memory_paths", {"query": "projects", "type": "context", "limit": 10}
    )
    assert calls[-1] == (
        "/api/v1/memories/paths",
        {"query": "projects", "type": "context", "limit": 10},
    )
    await server.call_tool("read_memory_path", {})
    assert calls[-1] == (
        "/api/v1/memories/path",
        {
            "path": "projects/app",
            "type": "all",
            "include_children": True,
            "limit": 50,
        },
    )
    await server.call_tool(
        "read_memory_path", {"path": "/projects/other/", "include_children": False}
    )
    assert calls[-1][1]["path"] == "projects/other"
    assert calls[-1][1]["include_children"] is False


@pytest.mark.parametrize("path", ["projects/../other", "./app", "a\x00b"])
async def test_invalid_paths_fail_without_http(path):
    with pytest.raises(ValueError):
        OpenWebUIConfig(url="http://webui", memory_path=path)
    server, calls = fixture_server()
    with pytest.raises(Exception, match="memory path|Memory paths"):
        await server.call_tool("search_memories", {"query": "x", "path": path})
    assert calls == []


@pytest.mark.parametrize(
    "results",
    [
        {"documents": [["secret"]]},
        {
            "ids": [["project"]],
            "documents": [["secret", "extra"]],
        },
    ],
)
async def test_scoped_search_rejects_unfilterable_results(results):
    def handler(request):
        return httpx.Response(200, json=MEMORIES if request.method == "GET" else results)

    server = create_server(
        OpenWebUIConfig(url="http://webui", memory_path="projects/app"),
        "key",
        httpx.MockTransport(handler),
    )
    with pytest.raises(Exception, match="IDs|misaligned"):
        await server.call_tool("search_memories", {"query": "x"})


def test_profiles_share_project_between_backends_and_allow_override(tmp_path):
    path = tmp_path / "profiles.toml"
    path.write_text("""
[openwebui]
url = "http://webui"
memory_path = "/projects/app/"
memory_include_shared = false
[profiles.claude]
cwd = "."
[profiles.codex]
backend = "codex"
cwd = "."
allowed_tools = []
[profiles.other]
cwd = "."
[profiles.other.openwebui]
memory_path = "projects/other"
""")
    profiles = load_settings(path).profiles
    assert profiles["claude"].openwebui == profiles["codex"].openwebui
    assert profiles["claude"].openwebui.memory_path == "projects/app"
    assert not profiles["claude"].openwebui.memory_include_shared
    assert profiles["other"].openwebui.memory_path == "projects/other"
