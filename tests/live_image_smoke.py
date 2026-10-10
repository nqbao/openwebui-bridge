"""Opt-in authenticated image/resume test; excluded from pytest discovery."""

import argparse
import asyncio
import base64
import json
import struct
import tempfile
import zlib
from pathlib import Path

import httpx

from openwebui_bridge.app import create_app
from openwebui_bridge.backends import ClaudeBackend, CodexBackend
from openwebui_bridge.config import Profile, Settings


def test_image(reverse=False):
    # Generate a real PNG with a red left half and a blue right half, without
    # writing an attachment to disk or adding an image-processing dependency.
    def chunk(kind, data):
        return (
            struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data))
        )

    halves = [b"\xff\x00\x00" * 128, b"\x00\x00\xff" * 128]
    row = b"\x00" + b"".join(reversed(halves) if reverse else halves)
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack("!IIBBBBB", 256, 128, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(row * 128)) + chunk(b"IEND", b"")
    return "data:image/png;base64," + base64.b64encode(png).decode()


async def main(backend_name="claude"):
    backend_class = ClaudeBackend if backend_name == "claude" else CodexBackend

    class RecordingBackend(backend_class):
        def __init__(self):
            self.calls = []

        async def stream(self, profile, prompt, resume):
            self.calls.append((prompt, resume))
            async for event in super().stream(profile, prompt, resume):
                yield event

    with tempfile.TemporaryDirectory(prefix="openwebui-bridge-image-smoke-") as directory:
        root = Path(directory)
        backend = RecordingBackend()
        settings = Settings(
            profiles={
                "agent:vision": Profile(
                    cwd=root,
                    backend=backend_name,
                    model="sonnet" if backend_name == "claude" else None,
                    allowed_tools=[],
                )
            },
            database=root / "sessions.db",
            turn_timeout_seconds=90,
        )
        app = create_app(settings, {backend_name: backend})
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            history = [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Name the color of each half of this image, left then right. Reply with only the two color names.",
                        },
                        {"type": "image_url", "image_url": {"url": test_image()}},
                    ],
                }
            ]
            for turn in range(3):
                response = await client.post(
                    "/v1/chat/completions",
                    headers={"X-OpenWebUI-Chat-Id": "vision"},
                    json={"model": "agent:vision", "stream": True, "messages": history},
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
                print(f"Vision turn {turn + 1}: {text}")
                assert "red" in text.lower() and "blue" in text.lower()
                first, second = ("blue", "red") if turn == 2 else ("red", "blue")
                assert text.lower().index(first) < text.lower().index(second)
                history += [
                    {"role": "assistant", "content": text},
                    {
                        "role": "user",
                        "content": "What were those colors, left then right? Reply with only the two color names.",
                    },
                ]
                if turn == 1:
                    history[-1]["content"] = [
                        {
                            "type": "text",
                            "text": "Here is a new image. Name its colors, left then right, using only two color names. Do not use tools.",
                        },
                        {"type": "image_url", "image_url": {"url": test_image(reverse=True)}},
                    ]
            assert backend.calls[0][1] is None
            assert backend.calls[1][1] is not None
            assert isinstance(backend.calls[1][0], str)
            assert backend.calls[2][1] is not None
            assert isinstance(backend.calls[2][0], list)
            print(
                f"Authenticated {backend_name} image input, native resume, and new image on resume passed."
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=["claude", "codex"], default="claude")
    asyncio.run(main(parser.parse_args().backend))
