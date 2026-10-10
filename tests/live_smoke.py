"""Opt-in authenticated Claude smoke test; excluded from pytest discovery."""

import asyncio
import json
import tempfile
from pathlib import Path

import httpx

from openwebui_bridge.app import create_app
from openwebui_bridge.backends import ClaudeBackend
from openwebui_bridge.config import Profile, Settings


async def main():
    class RecordingClaude(ClaudeBackend):
        def __init__(self):
            self.calls = []

        async def stream(self, profile, prompt, resume):
            self.calls.append((prompt, resume))
            async for event in super().stream(profile, prompt, resume):
                yield event

    with tempfile.TemporaryDirectory(prefix="openwebui-bridge-smoke-") as directory:
        root = Path(directory)
        backend = RecordingClaude()
        settings = Settings(
            profiles={"claude-code:smoke": Profile(cwd=root, model="sonnet", allowed_tools=[])},
            database=root / "sessions.sqlite3",
            turn_timeout_seconds=60,
        )
        app = create_app(settings, {"claude": backend})
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            history = [
                {
                    "role": "user",
                    "content": "For this conversation only, the token is maple-731. "
                    "Do not save it to files or use tools. Reply with just OK.",
                }
            ]
            for turn in range(2):
                response = await client.post(
                    "/v1/chat/completions",
                    headers={"X-OpenWebUI-Chat-Id": "smoke", "X-OpenWebUI-User-Id": "local"},
                    json={"model": "claude-code:smoke", "stream": True, "messages": history},
                )
                response.raise_for_status()
                text = ""
                for line in response.text.splitlines():
                    if not line.startswith("data: ") or line == "data: [DONE]":
                        continue
                    chunk = json.loads(line[6:])
                    if "error" in chunk:
                        raise RuntimeError(chunk["error"]["message"])
                    text += chunk["choices"][0]["delta"].get("content", "")
                print(f"Turn {turn + 1}: {text}")
                history.append({"role": "assistant", "content": text})
                history.append(
                    {
                        "role": "user",
                        "content": "What token did I ask you to remember? Reply with just the token.",
                    }
                )
                if turn == 0:
                    before = [
                        tuple(row) for row in app.state.store.db.execute("SELECT * FROM sessions")
                    ]
                    task_response = await client.post(
                        "/v1/chat/completions",
                        headers={
                            "X-OpenWebUI-Chat-Id": "smoke",
                            "X-OpenWebUI-User-Id": "local",
                            "X-OpenWebUI-Task": "title_generation",
                        },
                        json={
                            "model": "claude-code:smoke",
                            "stream": False,
                            "messages": [
                                {"role": "user", "content": "Return exactly: Proxy smoke test"}
                            ],
                        },
                    )
                    task_response.raise_for_status()
                    assert task_response.json()["object"] == "chat.completion"
                    task_text = task_response.json()["choices"][0]["message"]["content"]
                    assert "Proxy smoke test" in task_text
                    assert [
                        tuple(row) for row in app.state.store.db.execute("SELECT * FROM sessions")
                    ] == before
                    assert backend.calls[1][1] is None
                    print(f"Isolated Haiku task: {task_text}")
            assert backend.calls[0][1] is None
            assert backend.calls[-1][1] is not None, "Second turn did not resume"
            assert "maple-731" not in backend.calls[-1][0], "Second prompt replayed history"
            assert "maple-731" in text, "Resumed agent did not remember the token"
            print("Authenticated streaming, isolated JSON task, and native session resume passed.")


if __name__ == "__main__":
    asyncio.run(main())
