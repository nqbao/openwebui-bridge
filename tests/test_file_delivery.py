import json
import tomllib
from pathlib import Path

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from openwebui_bridge.app import create_app
from openwebui_bridge.backends import Done, Text, codex_command
from openwebui_bridge.config import OpenWebUIConfig, Settings, load_settings
from openwebui_bridge.file_delivery import DOWNLOADS_ENV, MAX_FILE_BYTES, download_url
from openwebui_bridge.openwebui_mcp import connection_env, create_server
from openwebui_bridge.protocol import session_digest


def test_send_file_is_opt_in_and_output_path_is_relative_to_config(tmp_path):
    assert "send_file" not in OpenWebUIConfig(url="http://webui").tool_names()
    path = tmp_path / "profiles.toml"
    path.write_text("""
[openwebui]
url = "http://webui"
send_files = true
file_output_dir = "outputs"
[profiles.agent]
cwd = "."
backend = "codex"
allowed_tools = []
""")
    profile = load_settings(path).profiles["agent"]
    assert profile.openwebui.file_output_dir == tmp_path / "outputs"
    command = codex_command(profile, None)
    stanza = next(arg for arg in command if arg.startswith("mcp_servers.openwebui="))
    server = tomllib.loads(stanza)["mcp_servers"]["openwebui"]
    assert DOWNLOADS_ENV in server["env_vars"]
    assert "send_file" in server["enabled_tools"]
    assert 'mcp_servers.openwebui.tools.send_file.approval_mode="approve"' in command


async def test_send_file_uploads_text_and_binary(tmp_path, monkeypatch):
    root = tmp_path / "outputs"
    root.mkdir()
    (root / "report.zip").write_bytes(b"PK\x03\x04test")
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    monkeypatch.setenv(DOWNLOADS_ENV, str(receipts))
    uploads = []

    def upload(request):
        assert request.method == "POST" and request.url.path == "/api/v1/files/"
        assert request.url.params["process"] == "false"
        assert request.headers["Authorization"] == "Bearer key"
        uploads.append(request.read())
        return httpx.Response(200, json={"id": f"file-{len(uploads)}"})

    config = OpenWebUIConfig(url="http://webui", send_files=True, file_output_dir=root)
    server = create_server(config, "key", httpx.MockTransport(upload))
    text = await server.call_tool("send_file", {"filename": "report.csv", "text": "café,1"})
    binary = await server.call_tool("send_file", {"filename": "report.zip"})
    assert not text.is_error and not binary.is_error
    assert json.loads(text.content[0].text) == {
        "filename": "report.csv",
        "url": download_url("file-1"),
    }
    assert b'filename="report.csv"' in uploads[0] and "café,1".encode() in uploads[0]
    assert b'filename="report.zip"' in uploads[1] and b"PK\x03\x04test" in uploads[1]
    assert (receipts / "file-1").read_text() == "report.csv"
    assert (receipts / "file-2").read_text() == "report.zip"


async def test_send_file_rejects_path_escape_and_oversize(tmp_path):
    root = tmp_path / "outputs"
    root.mkdir()
    private = tmp_path / "private"
    private.write_bytes(b"private")
    (root / "linked").symlink_to(private)
    with (root / "large").open("wb") as f:
        f.truncate(MAX_FILE_BYTES + 1)

    def unexpected_upload(_request):
        pytest.fail("Rejected files must not reach Open WebUI")

    config = OpenWebUIConfig(url="http://webui", send_files=True, file_output_dir=root)
    server = create_server(config, "key", httpx.MockTransport(unexpected_upload))
    for name in (str(private), "../private", "linked", "large", "missing"):
        with pytest.raises(ToolError, match="Error executing tool send_file"):
            await server.call_tool("send_file", {"filename": name})


async def test_failed_upload_creates_no_download_receipt(tmp_path, monkeypatch):
    monkeypatch.setenv(DOWNLOADS_ENV, str(tmp_path))
    config = OpenWebUIConfig(url="http://webui", send_files=True)
    transport = httpx.MockTransport(lambda request: httpx.Response(403))
    server = create_server(config, "key", transport)
    with pytest.raises(ToolError, match="HTTP 403"):
        await server.call_tool("send_file", {"filename": "report.csv", "text": ""})
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("stream", [False, True])
async def test_successful_send_file_appears_in_reply_and_session(tmp_path, monkeypatch, stream):
    monkeypatch.setenv("OPENWEBUI_API_KEY", "key")
    path = tmp_path / "profiles.toml"
    path.write_text("""
[openwebui]
url = "http://webui"
send_files = true
[profiles.agent]
cwd = "."
""")
    config = load_settings(path).profiles["agent"]
    uploads = []
    turn_directories = []

    class Backend:
        async def stream(self, profile, prompt, resume):
            # The real backend passes this environment to its stdio MCP worker.
            receipts = connection_env(profile.openwebui)[DOWNLOADS_ENV]
            turn_directories.append(Path(receipts))
            if "first" in prompt:
                with monkeypatch.context() as context:
                    context.setenv(DOWNLOADS_ENV, receipts)

                    def upload(request):
                        uploads.append(request)
                        return httpx.Response(200, json={"id": "file-1"})

                    server = create_server(profile.openwebui, "key", httpx.MockTransport(upload))
                    sent = await server.call_tool(
                        "send_file", {"filename": "test.csv", "text": "a,b"}
                    )
                    assert not sent.is_error
            yield Text("Done.")
            yield Done("native")

    settings = Settings(database=tmp_path / "sessions.sqlite3", profiles={"agent": config})
    app = create_app(settings, {"claude": Backend()})
    headers = {"X-OpenWebUI-Task": "", "X-OpenWebUI-Chat-Id": "chat"}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://bridge"
        ) as client,
    ):
        messages = [{"role": "user", "content": "first"}]
        response = await client.post(
            "/v1/chat/completions",
            headers=headers,
            json={"model": "agent", "stream": stream, "messages": messages},
        )
        if stream:
            events = [
                json.loads(line[6:])
                for line in response.text.splitlines()
                if line.startswith("data: ") and line != "data: [DONE]"
            ]
            content = "".join(e["choices"][0]["delta"].get("content", "") for e in events)
        else:
            content = response.json()["choices"][0]["message"]["content"]
        assert len(uploads) == 1
        assert content.count(f"[Download test.csv]({download_url('file-1')})") == 1
        assert all(not directory.exists() for directory in turn_directories)
        saved = app.state.store.db.execute("SELECT prefix_hash FROM sessions").fetchone()[0]
        assert saved == session_digest(messages + [{"role": "assistant", "content": content}])
