import base64
import io
import json
import sys
from contextlib import asynccontextmanager

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from PIL import Image as PILImage

from openwebui_bridge.app import create_app
from openwebui_bridge.backends import (
    CodexBackend,
    CodexImageLog,
    CodexImages,
    Done,
    Image,
    Text,
)
from openwebui_bridge.config import OpenWebUIConfig, Profile, Settings
from openwebui_bridge.images import (
    MAX_IMAGE_BYTES,
    ImageDeliveryError,
    decode_image,
)
from openwebui_bridge.openwebui_mcp import OpenWebUIClient
from openwebui_bridge.protocol import digest, replay, uploaded_image_ids


def png(color="green"):
    output = io.BytesIO()
    PILImage.new("RGB", (4, 4), color).save(output, format="PNG")
    return output.getvalue()


def generation(data=None, **overrides):
    # Shape captured from codex-cli 0.166.0's real dinosaur generation.
    return {
        "type": "Extension",
        "kind": "image_gen.generation",
        "id": "exec-5773961d-0ac3-4f4e-a9ab-61c76d075878",
        "status": "completed",
        "revisedPrompt": "Create a single adorable dinosaur emoji",
        "result": base64.b64encode(data or png()).decode(),
        "transparentBackground": True,
        "failure": None,
        "savedPath": "/must/not/be/opened.png",
        "generationId": "1a95b40a-0ab4-4984-9728-3f014ef77e01",
        **overrides,
    }


def test_observed_completed_generation_and_dedup():
    parser = CodexImages()
    assert parser.parse(generation(status="in_progress")) == []
    assert parser.parse(generation()) == [Image(png(), "image/png")]
    assert parser.parse(generation(id="different-id")) == []
    with pytest.raises(ImageDeliveryError, match="no image payload"):
        parser.parse(generation(result=None))


@pytest.mark.parametrize(
    "value,mime",
    [
        ("not base64!", "image/png"),
        (base64.b64encode(b"\x89PNG\r\n\x1a\ntruncated").decode(), "image/png"),
        (base64.b64encode(png()).decode(), "image/jpeg"),
        (base64.b64encode(b"<svg></svg>").decode(), "image/svg+xml"),
        ("A" * (4 * ((MAX_IMAGE_BYTES + 2) // 3) + 101), "image/png"),
        (None, "image/png"),
    ],
)
def test_invalid_and_oversized_images(value, mime):
    with pytest.raises(ImageDeliveryError):
        decode_image(value, mime)


def test_mcp_resources_are_not_arbitrary_file_or_network_access():
    parser = CodexImages()
    result = {
        "type": "mcp_tool_call",
        "result": {
            "content": [
                {"type": "resource_link", "uri": "file:///etc/passwd", "mimeType": "image/png"}
            ]
        },
    }
    assert parser.parse(result) == []
    result["result"]["content"] = [
        {
            "type": "resource",
            "resource": {
                "uri": "file:///must/not/open",
                "mimeType": "image/png",
                "blob": base64.b64encode(png()).decode(),
            },
        }
    ]
    assert parser.parse(result) == [Image(png(), "image/png")]


async def test_completed_mcp_images_after_activity_and_duplicate_events(tmp_path, monkeypatch):
    item = {"id": "tool", "type": "mcp_tool_call", "server": "images", "tool": "generate"}
    completed = {
        **item,
        "status": "completed",
        "result": {
            "content": [
                {"type": "image", "data": base64.b64encode(png()).decode(), "mimeType": "image/png"}
            ]
        },
    }
    events = [
        {"type": "thread.started", "thread_id": "native"},
        {"type": "item.started", "item": item},
        {"type": "item.completed", "item": completed},
        {"type": "item.completed", "item": completed},
        {"type": "turn.completed"},
    ]
    script = "import sys; sys.stdin.read(); print(" + repr("\n".join(map(json.dumps, events))) + ")"
    monkeypatch.setattr(
        "openwebui_bridge.backends.codex_command", lambda *_: [sys.executable, "-c", script]
    )
    profile = Profile(cwd=tmp_path, backend="codex", allowed_tools=[], codex_image_log=False)
    result = [e async for e in CodexBackend().stream(profile, "generate", None)]
    assert isinstance(result[0], Text)
    assert result[1:] == [Image(png(), "image/png"), Done("native")]


def record(item, session, turn="turn"):
    return {
        "type": "event_msg",
        "payload": {"type": "item_completed", "thread_id": session, "turn_id": turn, "item": item},
    }


def test_log_supplement_order_resume_and_partial_records(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    session = "01a1adb2-32b7-7182-a69e-f0af38e82f80"
    directory = tmp_path / "sessions/2026/11/05"
    directory.mkdir(parents=True)
    path = directory / f"rollout-date-{session}.jsonl"
    start = {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn"}}
    message = lambda text: {"type": "AgentMessage", "content": [{"type": "Text", "text": text}]}
    records = [
        start,
        record(message("before"), session),
        record(generation(), session),
        record(message("after"), session),
    ]
    path.write_text("\n".join(map(json.dumps, records)) + "\n")
    log = CodexImageLog(None)
    assert log.read(session, "before") == []
    assert log.read(session, "after") == [generation()]
    assert log.read(session) == []
    resumed = CodexImageLog(session)
    assert resumed.read(session) == []
    content = json.dumps(start) + "\n" + json.dumps(record(generation(png("red")), session))
    with path.open("a") as file:
        file.write(content)
    assert resumed.read(session) == []  # Incomplete last JSON record is retried.
    with path.open("a") as file:
        file.write("\n")
    assert resumed.read(session) == [generation(png("red"))]
    assert CodexImageLog(None).locate("../../etc/passwd") is None


async def test_real_adapter_supplements_log_without_moving_image_before_text(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    session = "01a1adb2-32b7-7182-a69e-f0af38e82f80"
    directory = tmp_path / "sessions/2026/11/05"
    directory.mkdir(parents=True)
    native_message = lambda text: {
        "type": "AgentMessage",
        "content": [{"type": "Text", "text": text}],
    }
    records = [
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn"}},
        record(native_message("Before"), session),
        record(generation(), session),
        record(native_message("After"), session),
    ]
    (directory / f"rollout-date-{session}.jsonl").write_text(
        "\n".join(map(json.dumps, records)) + "\n"
    )
    # Deliberately let the entire native log exist before stdout is consumed.
    events = [
        {"type": "thread.started", "thread_id": session},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Before"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "After"}},
        {"type": "turn.completed"},
    ]
    script = "import sys; sys.stdin.read(); print(" + repr("\n".join(map(json.dumps, events))) + ")"
    monkeypatch.setattr(
        "openwebui_bridge.backends.codex_command", lambda *_: [sys.executable, "-c", script]
    )
    profile = Profile(cwd=tmp_path, backend="codex", allowed_tools=[])
    assert [e async for e in CodexBackend().stream(profile, "generate", None)] == [
        Text("Before"),
        Image(png(), "image/png"),
        Text("After"),
        Done(session),
    ]


class ImageBackend:
    def __init__(self):
        self.calls = []

    async def stream(self, profile, prompt, resume):
        self.calls.append((profile, prompt, resume))
        yield Text("Before")
        yield Image(png(), "image/png", "Dinosaur [cute]")
        yield Text("After")
        yield Done("native")


@asynccontextmanager
async def image_client(tmp_path, openwebui=None, **overrides):
    profile = Profile(cwd=tmp_path, backend="codex", allowed_tools=[], openwebui=openwebui)
    settings = Settings(
        profiles={"codex:test": profile},
        database=tmp_path / "sessions.db",
        api_key="private",
        **overrides,
    )
    backend = ImageBackend()
    app = create_app(settings, {"codex": backend})
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield client, backend, app


@pytest.mark.parametrize("stream", [True, False])
async def test_images_render_inline_and_persist_exactly(tmp_path, stream):
    async with image_client(tmp_path) as (client, backend, app):
        headers = {
            "Authorization": "Bearer private",
            "X-OpenWebUI-Task": "",
            "X-OpenWebUI-Chat-Id": "inline-chat",
        }
        messages = [{"role": "user", "content": "Generate a dinosaur"}]
        response = await client.post(
            "/v1/chat/completions",
            headers=headers,
            json={"model": "codex:test", "stream": stream, "messages": messages},
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
        assert "data:image/png;base64," + base64.b64encode(png()).decode() in content
        assert "https://" not in content
        saved = app.state.store.db.execute("SELECT prefix_hash FROM sessions").fetchone()[0]
        assert saved == digest(messages + [{"role": "assistant", "content": content}])
        history = messages + [
            {"role": "assistant", "content": content},
            {"role": "user", "content": "Make it blue"},
        ]
        await client.post(
            "/v1/chat/completions",
            headers=headers,
            json={"model": "codex:test", "stream": stream, "messages": history},
        )
        assert backend.calls[-1][1:] == ("Make it blue", "native")
        blocks = replay(history)
        assert isinstance(blocks, list)
        images = [b for b in blocks if b["type"] == "image"]
        assert images[0]["source"]["data"] == base64.b64encode(png()).decode()
        assert not any("data:image/" in b.get("text", "") for b in blocks)


def test_replay_leaves_corrupt_historical_image_as_text():
    bad = "![x](data:image/png;base64,AAAA)"
    blocks = replay([{"role": "assistant", "content": f"before {bad} after"}])
    assert blocks is not None
    assert bad in str(blocks)


OPENWEBUI = OpenWebUIConfig(url="https://webui.example", knowledge=False, memory=False)


async def test_upload_image_posts_multipart_and_returns_same_origin_url():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = request.content
        return httpx.Response(200, json={"id": "0b1c-2d3e"})

    client = OpenWebUIClient(OPENWEBUI, "secret", transport=httpx.MockTransport(handler))
    url = await client.upload_image(png(), "image/png")
    assert url == "/api/v1/files/0b1c-2d3e/content"
    assert seen["url"] == "https://webui.example/api/v1/files/?process=false"
    assert seen["auth"] == "Bearer secret"
    assert png() in seen["body"] and b"generated.png" in seen["body"]


async def test_upload_image_rejects_unsafe_file_id():
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"id": "../x"}))
    with pytest.raises(ToolError):
        await OpenWebUIClient(OPENWEBUI, "secret", transport=transport).upload_image(
            png(), "image/png"
        )


@pytest.mark.parametrize("stream", [True, False])
async def test_images_upload_to_openwebui_by_default(tmp_path, monkeypatch, stream):
    monkeypatch.setenv("OPENWEBUI_API_KEY", "secret")
    uploads = []

    async def upload(self, data, mime_type):
        uploads.append((data, mime_type))
        return "/api/v1/files/abc-123/content"

    monkeypatch.setattr(OpenWebUIClient, "upload_image", upload)
    async with image_client(tmp_path, openwebui=OPENWEBUI) as (client, _, app):
        headers = {
            "Authorization": "Bearer private",
            "X-OpenWebUI-Task": "",
            "X-OpenWebUI-Chat-Id": "chat",
        }
        messages = [{"role": "user", "content": "Draw"}]
        response = await client.post(
            "/v1/chat/completions",
            headers=headers,
            json={"model": "codex:test", "stream": stream, "messages": messages},
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
        assert uploads == [(png(), "image/png")]
        assert "(/api/v1/files/abc-123/content)" in content and "data:image" not in content
        saved = app.state.store.db.execute("SELECT prefix_hash FROM sessions").fetchone()[0]
        assert saved == digest(messages + [{"role": "assistant", "content": content}])


@pytest.mark.parametrize("failure", ["upload", "no_key", "disabled"])
async def test_images_fall_back_to_inline(tmp_path, monkeypatch, failure):
    config = OPENWEBUI
    if failure == "no_key":
        monkeypatch.delenv("OPENWEBUI_API_KEY", raising=False)
    else:
        monkeypatch.setenv("OPENWEBUI_API_KEY", "secret")
    if failure == "disabled":
        config = OPENWEBUI.model_copy(update={"upload_images": False})

    async def broken(self, data, mime_type):
        raise ToolError("Open WebUI returned HTTP 500")

    monkeypatch.setattr(OpenWebUIClient, "upload_image", broken)
    async with image_client(tmp_path, openwebui=config) as (client, _, _):
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer private", "X-OpenWebUI-Task": ""},
            json={
                "model": "codex:test",
                "stream": False,
                "messages": [{"role": "user", "content": "Draw"}],
            },
        )
        content = response.json()["choices"][0]["message"]["content"]
        assert "data:image/png;base64," + base64.b64encode(png()).decode() in content


async def test_download_image_validates_and_limits():
    ok = OpenWebUIClient(
        OPENWEBUI,
        "secret",
        transport=httpx.MockTransport(
            lambda r: (
                httpx.Response(200, content=png())
                if r.url.path == "/api/v1/files/abc/content"
                and r.headers["authorization"] == "Bearer secret"
                else httpx.Response(404)
            )
        ),
    )
    assert await ok.download_image("abc") == (png(), "image/png")
    with pytest.raises(ToolError, match="HTTP 404"):
        await ok.download_image("missing")
    not_image = OpenWebUIClient(
        OPENWEBUI,
        "secret",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"x")),
    )
    with pytest.raises(ToolError, match="invalid or unsupported"):
        await not_image.download_image("abc")
    with pytest.raises(ToolError):
        await ok.download_image("../etc")


def test_replay_uses_fetched_uploaded_images_and_ignores_other_urls():
    link = "![d](/api/v1/files/abc-1/content)"
    other = "![x](https://evil.example/api/v1/files/abc-1/content)"
    history = [
        {"role": "user", "content": "Draw"},
        {"role": "assistant", "content": f"{link} {other}"},
        {"role": "user", "content": "Edit it"},
    ]
    assert uploaded_image_ids(history) == ["abc-1"]
    blocks = replay(history, {"abc-1": (png(), "image/png")})
    images = [b for b in blocks if b["type"] == "image"]
    assert len(images) == 1 and images[0]["source"]["media_type"] == "image/png"
    assert other in str(blocks)
    # Without the fetched bytes the link stays as plain text.
    assert isinstance(replay(history), str) and link in replay(history)


async def test_fresh_session_fetches_uploaded_images(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENWEBUI_API_KEY", "secret")
    fetched = []

    async def download(self, file_id):
        fetched.append(file_id)
        return png(), "image/png"

    monkeypatch.setattr(OpenWebUIClient, "download_image", download)
    async with image_client(tmp_path, openwebui=OPENWEBUI) as (client, backend, _):
        history = [
            {"role": "user", "content": "Draw"},
            {"role": "assistant", "content": "![d](/api/v1/files/abc-1/content)"},
            {"role": "user", "content": "Make it blue"},
        ]
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer private", "X-OpenWebUI-Task": ""},
            json={"model": "codex:test", "stream": False, "messages": history},
        )
        assert response.status_code == 200
        assert fetched == ["abc-1"]
        assert any(b["type"] == "image" for b in backend.calls[-1][1])

    async def broken(self, file_id):
        raise ToolError("Open WebUI returned HTTP 404")

    monkeypatch.setattr(OpenWebUIClient, "download_image", broken)
    async with image_client(tmp_path, openwebui=OPENWEBUI) as (client, backend, _):
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer private", "X-OpenWebUI-Task": ""},
            json={"model": "codex:test", "stream": False, "messages": history},
        )
        assert response.status_code == 200
        assert "/api/v1/files/abc-1/content" in str(backend.calls[-1][1])


@pytest.mark.parametrize(
    "header, declared",
    [
        ("data:image/PNG;base64", None),
        ("data:image/png;charset=utf-8;base64", "image/PNG"),
        ("data:image/png; base64", None),
    ],
)
def test_data_urls_ignore_case_and_parameters(header, declared):
    value = header + "," + base64.b64encode(png()).decode()
    if header.endswith("; base64"):
        with pytest.raises(ImageDeliveryError):
            decode_image(value, declared)
    else:
        assert decode_image(value, declared) == (png(), "image/png")
    with pytest.raises(ImageDeliveryError, match="disagree"):
        decode_image("data:image/png;base64," + base64.b64encode(png()).decode(), "image/jpeg")


def test_mpo_jpeg_is_accepted_as_jpeg():
    output = io.BytesIO()
    PILImage.new("RGB", (4, 4), "red").save(output, format="MPO")
    data = output.getvalue()
    assert PILImage.open(io.BytesIO(data)).format in {"MPO", "JPEG"}
    assert decode_image(base64.b64encode(data).decode(), "image/jpeg")[1] == "image/jpeg"
