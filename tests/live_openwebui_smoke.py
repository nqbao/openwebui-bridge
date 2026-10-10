"""Authenticated agent/MCP smoke against a disposable Open WebUI API fixture.

No requests go to a real Open WebUI instance and no real memories are modified.
"""

import argparse
import asyncio
import json
import os
import secrets
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from openwebui_bridge.app import create_app
from openwebui_bridge.backends import ClaudeBackend, CodexBackend
from openwebui_bridge.config import OpenWebUIConfig, Profile, Settings


async def smoke(backend_name):
    knowledge_value = "knowledge-" + secrets.token_hex(4)
    memory_value = "memory-" + secrets.token_hex(4)
    memories = [{"id": "existing", "content": memory_value}]
    calls = []
    key = "fixture-key-" + secrets.token_hex(8)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def respond(self, value, status=200):
            data = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def handle_api(self):
            path = urlsplit(self.path).path
            if self.headers.get("Authorization") != "Bearer " + key:
                self.respond({"detail": "Unauthorized"}, 401)
                return
            body = json.loads(
                self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}"
            )
            calls.append((self.command, path, body))
            if path == "/api/v1/knowledge/":
                self.respond(
                    {
                        "items": [
                            {
                                "id": "kb",
                                "name": "Fixture Knowledge",
                                "description": "smoke reference",
                            }
                        ],
                        "total": 1,
                    }
                )
            elif path == "/api/v1/knowledge/kb":
                self.respond({"id": "kb", "name": "Fixture Knowledge", "files": None})
            elif path == "/api/v1/knowledge/kb/files":
                self.respond({"items": [{"id": "file", "filename": "reference.txt"}], "total": 1})
            elif path == "/api/v1/retrieval/query/collection":
                assert body["collection_names"] == ["kb"]
                self.respond(
                    {
                        "documents": [[knowledge_value]],
                        "metadatas": [[{"file_id": "file", "name": "reference.txt"}]],
                    }
                )
            elif path == "/api/v1/files/file/data/content":
                self.respond({"content": "The Knowledge verification value is " + knowledge_value})
            elif path == "/api/v1/memories/":
                self.respond(memories)
            elif path == "/api/v1/memories/query":
                self.respond(
                    {
                        "documents": [[item["content"] for item in memories]],
                        "ids": [[item["id"] for item in memories]],
                    }
                )
            elif path == "/api/v1/memories/add":
                item = {"id": "temporary", "content": body["content"]}
                memories.append(item)
                self.respond(item)
            elif path == "/api/v1/memories/temporary/update":
                item = next(item for item in memories if item["id"] == "temporary")
                item["content"] = body["content"]
                self.respond(item)
            elif path == "/api/v1/memories/temporary" and self.command == "DELETE":
                memories[:] = [item for item in memories if item["id"] != "temporary"]
                self.respond(True)
            else:
                self.respond({"detail": "Not found"}, 404)

        do_GET = handle_api
        do_POST = handle_api
        do_DELETE = handle_api

    base = ClaudeBackend if backend_name == "claude" else CodexBackend

    class RecordingBackend(base):
        def __init__(self):
            self.resumes = []

        async def stream(self, profile, prompt, resume):
            self.resumes.append(resume)
            async for event in super().stream(profile, prompt, resume):
                yield event

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    os.environ["BRIDGE_SMOKE_WEBUI_KEY"] = key
    try:
        with tempfile.TemporaryDirectory(prefix="openwebui-bridge-mcp-smoke-") as directory:
            root = Path(directory)
            profile = Profile(
                cwd=root,
                backend=backend_name,
                allowed_tools=[],
                model="sonnet" if backend_name == "claude" else None,
                openwebui=OpenWebUIConfig(
                    url=f"http://127.0.0.1:{httpd.server_port}",
                    api_key_env="BRIDGE_SMOKE_WEBUI_KEY",
                    memory_write=True,
                ),
            )
            backend = RecordingBackend()
            app = create_app(
                Settings(
                    profiles={"agent:test": profile},
                    database=root / "sessions.db",
                    turn_timeout_seconds=180,
                ),
                {backend_name: backend},
            )
            history = [
                {
                    "role": "user",
                    "content": "Verify the Open WebUI integration using ONLY the openwebui MCP tools. "
                    "Call list_knowledge, search_knowledge for verification value, list_knowledge_files, "
                    "read_knowledge_file, list_memories, and search_memories for verification value. "
                    "Actually call all six tools. Reply with the Knowledge and Memory verification values you found.",
                }
            ]
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client,
            ):
                for turn in range(2):
                    response = await client.post(
                        "/v1/chat/completions",
                        headers={"X-OpenWebUI-Chat-Id": "smoke"},
                        json={"model": "agent:test", "stream": True, "messages": history},
                    )
                    response.raise_for_status()
                    text = ""
                    for line in response.text.splitlines():
                        if not line.startswith("data: ") or line == "data: [DONE]":
                            continue
                        event = json.loads(line[6:])
                        if "error" in event:
                            raise RuntimeError(event["error"]["message"])
                        text += event["choices"][0]["delta"].get("content", "")
                    print(f"{backend_name} turn {turn + 1}: {text}", flush=True)
                    if turn == 0:
                        assert knowledge_value in text and memory_value in text
                        paths = {path for _, path, _ in calls}
                        assert {
                            "/api/v1/knowledge/",
                            "/api/v1/retrieval/query/collection",
                            "/api/v1/knowledge/kb/files",
                            "/api/v1/files/file/data/content",
                            "/api/v1/memories/",
                            "/api/v1/memories/query",
                        } <= paths
                    history += [
                        {"role": "assistant", "content": text},
                        {
                            "role": "user",
                            "content": "Using ONLY Open WebUI MCP tools, remember a NEW temporary memory with content 'smoke original'. "
                            "Then update ONLY that newly created memory to 'smoke corrected'. "
                            "Then forget ONLY that same temporary memory, leaving existing memories alone. "
                            "List memories to verify the existing memory is still there. Actually perform all four tool calls.",
                        },
                    ]
                assert backend.resumes[0] is None and backend.resumes[1] is not None
                paths = {path for _, path, _ in calls}
                assert {
                    "/api/v1/memories/add",
                    "/api/v1/memories/temporary/update",
                    "/api/v1/memories/temporary",
                } <= paths
                assert memories == [{"id": "existing", "content": memory_value}]
                print(
                    f"{backend_name}: Knowledge reads/search, Memory reads/search/writes, and native resume passed.",
                    flush=True,
                )
    finally:
        os.environ.pop("BRIDGE_SMOKE_WEBUI_KEY", None)
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


async def main(backends):
    for backend in backends:
        await smoke(backend)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=["claude", "codex", "both"], default="both")
    choice = parser.parse_args().backend
    asyncio.run(main(["claude", "codex"] if choice == "both" else [choice]))
