import asyncio
import base64
import os
import sys
from itertools import pairwise
from pathlib import Path

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock
from claude_agent_sdk.types import StreamEvent

from openwebui_bridge.backends import ClaudeBackend, CodexBackend, Done, Text, codex_command
from openwebui_bridge.config import Profile
from openwebui_bridge.protocol import tool_line


def event(kind, **fields):
    return StreamEvent("uuid", "session", {"type": kind, **fields})


def result(subtype="success"):
    return ResultMessage(subtype, 1, 1, subtype != "success", 1, "session")


def mock_client(monkeypatch, messages):
    class Client:
        options = None
        prompt = None
        closed = False

        def __init__(self, options):
            Client.options = options

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            Client.closed = True

        async def query(self, prompt):
            Client.prompt = prompt if isinstance(prompt, str) else [item async for item in prompt]

        async def receive_response(self):
            for message in messages:
                yield message

    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", Client)
    return Client


async def test_claude_token_stream_tool_inputs_and_no_duplicate_text(tmp_path, monkeypatch):
    client = mock_client(
        monkeypatch,
        [
            event("message_start"),
            event("content_block_delta", index=0, delta={"type": "text_delta", "text": "Hello"}),
            AssistantMessage([TextBlock("Hello")], "model"),
            event("message_start"),
            event(
                "content_block_start",
                index=0,
                content_block={
                    "type": "tool_use",
                    "id": "read-1",
                    "name": "Read",
                    "input": {},
                },
            ),
            event(
                "content_block_delta",
                index=0,
                delta={
                    "type": "input_json_delta",
                    "partial_json": '{"file_path":',
                },
            ),
            event(
                "content_block_delta",
                index=0,
                delta={
                    "type": "input_json_delta",
                    "partial_json": '"src/main.py"}',
                },
            ),
            event("content_block_stop", index=0),
            AssistantMessage(
                [ToolUseBlock("read-1", "Read", {"file_path": "src/main.py"})], "model"
            ),
            AssistantMessage([TextBlock("Fallback")], "model"),
            result(),
        ],
    )
    events = [e async for e in ClaudeBackend().stream(Profile(cwd=tmp_path), "prompt", "old")]
    assert events == [
        Text("Hello"),
        Text(tool_line("Read", {"file_path": "src/main.py"})),
        Text("Fallback"),
        Done("session"),
    ]
    assert client.closed
    assert client.options.resume == "old"
    assert client.options.tools == ["Glob", "Grep", "Read"]
    assert client.options.setting_sources == ["project"]
    assert client.options.strict_mcp_config
    assert client.options.verbatim_prompts
    hook = client.options.hooks["PreToolUse"][0].hooks[0]
    assert (await hook({"tool_name": "Bash"}, None, None))["hookSpecificOutput"][
        "permissionDecision"
    ] == "deny"
    assert await hook({"tool_name": "Read"}, None, None) == {}
    assert (await client.options.can_use_tool("Write", {}, None)).behavior == "deny"


async def test_claude_filters_subagent_output(tmp_path, monkeypatch):
    nested = event("content_block_delta", delta={"type": "text_delta", "text": "nested"})
    nested.parent_tool_use_id = "parent"
    mock_client(
        monkeypatch, [nested, AssistantMessage([TextBlock("nested")], "model", "parent"), result()]
    )
    assert [e async for e in ClaudeBackend().stream(Profile(cwd=tmp_path), "p", None)] == [
        Done("session")
    ]


async def test_claude_sends_images_as_structured_user_input(tmp_path, monkeypatch):
    client = mock_client(monkeypatch, [result()])
    blocks = [
        {"type": "text", "text": "Describe this screenshot"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "image"}},
    ]
    assert [e async for e in ClaudeBackend().stream(Profile(cwd=tmp_path), blocks, "old")] == [
        Done("session")
    ]
    assert client.prompt == [
        {"type": "user", "message": {"role": "user", "content": blocks}, "parent_tool_use_id": None}
    ]
    assert client.options.resume == "old"


async def test_claude_task_profile_disables_tools_and_coding_system_prompt(tmp_path, monkeypatch):
    client = mock_client(monkeypatch, [AssistantMessage([TextBlock("Title")], "model"), result()])
    profile = Profile(cwd=tmp_path, allowed_tools=[], system_prompt="Standalone task", max_turns=2)
    _ = [e async for e in ClaudeBackend().stream(profile, "p", None)]
    assert client.options.tools == []
    assert client.options.allowed_tools == []
    assert client.options.resume is None
    assert client.options.system_prompt == "Standalone task"
    hook = client.options.hooks["PreToolUse"][0].hooks[0]
    assert (await hook({"tool_name": "Read"}, None, None))["hookSpecificOutput"][
        "permissionDecision"
    ] == "deny"


@pytest.mark.parametrize(
    "messages",
    [
        [],
        [result("error_max_turns")],
        [AssistantMessage([], "model", error="authentication_failed")],
    ],
)
async def test_claude_failure_never_returns_done(tmp_path, monkeypatch, messages):
    client = mock_client(monkeypatch, messages)
    with pytest.raises(RuntimeError):
        _ = [e async for e in ClaudeBackend().stream(Profile(cwd=tmp_path), "p", None)]
    assert client.closed


def test_codex_resume_flags_precede_subcommand(tmp_path):
    profile = Profile(cwd=tmp_path, backend="codex", allowed_tools=[], model="configured-model")
    command = codex_command(profile, "session")
    assert command[-3:] == ["resume", "session", "-"]
    assert command.index("--sandbox") < command.index("resume")
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert 'approval_policy="never"' in command
    assert "--ignore-user-config" in command


@pytest.mark.parametrize("resume", [None, "session"])
def test_codex_image_flags_for_new_and_resumed_turns(tmp_path, resume):
    profile = Profile(cwd=tmp_path, backend="codex", allowed_tools=[])
    command = codex_command(profile, resume, ["/tmp/one.png", "/tmp/two.jpeg"])
    assert command[-4:] == ["--image=/tmp/one.png", "--image=/tmp/two.jpeg", "--", "-"]
    if resume:
        assert command.index("resume") < command.index("--image=/tmp/one.png")


@pytest.mark.parametrize("outcome", ["success", "failure", "cancel", "spawn_failure"])
async def test_codex_image_files_private_and_cleaned(tmp_path, monkeypatch, outcome):
    script = tmp_path / "images.py"
    script.write_text("""import json, pathlib, sys, time
for image in sys.argv[1:]:
    assert pathlib.Path(image).read_bytes() == b"image fixture"
prompt = sys.stdin.read()
assert "[Attached image 1]" in prompt and "[Attached image 2]" in prompt
assert "aW1hZ2UgZml4dHVyZQ==" not in prompt
print(json.dumps({"type":"thread.started", "thread_id":"native"}), flush=True)
print(json.dumps({"type":"item.completed", "item":{"type":"agent_message", "text":"seen"}}), flush=True)
if "cancel" in prompt:
    time.sleep(60)
elif "failure" in prompt:
    print(json.dumps({"type":"turn.failed"}), flush=True)
else:
    print(json.dumps({"type":"turn.completed"}), flush=True)
""")
    paths = []

    def command(_profile, _resume, images, gateway_tools=None):
        assert gateway_tools == []
        paths.extend(Path(image) for image in images)
        assert len(images) == 2
        assert all(path.stat().st_mode & 0o777 == 0o600 for path in paths)
        assert paths[0].parent.stat().st_mode & 0o777 == 0o700
        assert paths[0].parent != tmp_path
        if outcome == "spawn_failure":
            return [str(tmp_path / "missing-executable")]
        return [sys.executable, str(script), *images]

    monkeypatch.setattr("openwebui_bridge.backends.codex_command", command)
    block = {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.b64encode(b"image fixture").decode(),
        },
    }
    profile = Profile(cwd=tmp_path, backend="codex", allowed_tools=[])
    stream = CodexBackend().stream(profile, [{"type": "text", "text": outcome}, block, block], None)
    if outcome == "spawn_failure":
        with pytest.raises(FileNotFoundError):
            await anext(stream)
    else:
        assert await anext(stream) == Text("seen")
        assert all(path.exists() for path in paths)
        if outcome == "cancel":
            await asyncio.wait_for(stream.aclose(), 3)
        elif outcome == "failure":
            with pytest.raises(RuntimeError):
                await anext(stream)
        else:
            assert [event async for event in stream] == [Done("native")]
    assert paths and all(not path.parent.exists() for path in paths)


async def test_codex_real_subprocess_jsonl_adapter(tmp_path, monkeypatch):
    script = tmp_path / "fake_codex.py"
    script.write_text("""import json, sys
prompt = sys.stdin.read()
assert prompt == "hello"
for event in [
    {"type": "thread.started", "thread_id": "native-session"},
    {"type": "item.started", "item": {"id": "cmd", "type": "command_execution", "command": "ls"}},
    {"type": "item.completed", "item": {"id": "cmd", "type": "command_execution", "command": "ls"}},
    {"type": "item.completed", "item": {"id": "text", "type": "agent_message", "text": "Hello"}},
    {"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 2}},
]:
    print(json.dumps(event), flush=True)
""")
    monkeypatch.setattr(
        "openwebui_bridge.backends.codex_command", lambda *_args: [sys.executable, str(script)]
    )
    profile = Profile(cwd=tmp_path, backend="codex", allowed_tools=[])
    events = [e async for e in CodexBackend().stream(profile, "hello", None)]
    assert events == [
        Text(tool_line("command_execution", {"command": "ls"})),
        Text("Hello"),
        Done("native-session", {"input_tokens": 3, "output_tokens": 2}),
    ]


async def test_codex_cancel_terminates_process(tmp_path, monkeypatch):
    script = tmp_path / "fake_codex.py"
    pid_file = tmp_path / "pid"
    script.write_text("""import json, os, sys, time
sys.stdin.read()
with open("pid", "w") as file:
    file.write(str(os.getpid()))
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "partial"}}), flush=True)
time.sleep(60)
""")
    monkeypatch.setattr(
        "openwebui_bridge.backends.codex_command", lambda *_args: [sys.executable, str(script)]
    )
    profile = Profile(cwd=tmp_path, backend="codex", allowed_tools=[])
    stream = CodexBackend().stream(profile, "hello", None)
    assert await anext(stream) == Text("partial")
    pid = int(pid_file.read_text())
    await asyncio.wait_for(stream.aclose(), 3)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_codex_failure_no_success_result(tmp_path, monkeypatch):
    code = "import json; print(json.dumps({'type': 'turn.failed'}), flush=True)"
    monkeypatch.setattr(
        "openwebui_bridge.backends.codex_command", lambda *_args: [sys.executable, "-c", code]
    )
    profile = Profile(cwd=tmp_path, backend="codex", allowed_tools=[])
    with pytest.raises(RuntimeError):
        _ = [e async for e in CodexBackend().stream(profile, "hello", None)]


def test_project_instructions_toggle(tmp_path):
    enabled = codex_command(Profile(cwd=tmp_path, backend="codex", allowed_tools=[]), None)
    disabled = codex_command(
        Profile(cwd=tmp_path, backend="codex", allowed_tools=[], project_instructions=False), None
    )
    assert "project_doc_max_bytes=0" not in enabled
    assert "project_doc_max_bytes=0" in disabled


async def test_project_mcp_opt_in(tmp_path, monkeypatch):
    profile = Profile(
        cwd=tmp_path,
        project_mcp=True,
        allowed_tools=["Read", "mcp__docs", "mcp__git__log", "mcp__wiki*"],
    )
    client = mock_client(monkeypatch, [result()])
    [e async for e in ClaudeBackend().stream(profile, "p", None)]
    assert not client.options.strict_mcp_config
    assert client.options.setting_sources == ["project", "local"]
    assert client.options.tools == ["Read"]
    assert {"mcp__docs", "mcp__git", "mcp__wiki"} <= set(client.options.allowed_tools)
    hook = client.options.hooks["PreToolUse"][0].hooks[0]
    for name in ("Read", "mcp__docs__search", "mcp__git__log", "mcp__wiki__x", "mcp__wiki2__y"):
        assert await hook({"tool_name": name}, None, None) == {}
    for name in ("mcp__git__push", "mcp__other__x", "mcp__openwebui__x", "Bash"):
        assert "hookSpecificOutput" in await hook({"tool_name": name}, None, None)


def test_project_mcp_validation(tmp_path):
    with pytest.raises(ValueError):
        Profile(cwd=tmp_path, allowed_tools=["mcp__docs"])
    with pytest.raises(ValueError):
        Profile(cwd=tmp_path, project_mcp=True, allowed_tools=["mcp__openwebui__x"])
    with pytest.raises(ValueError):
        Profile(cwd=tmp_path, backend="codex", allowed_tools=["mcp__docs"])
    with pytest.raises(ValueError):
        Profile(
            cwd=tmp_path,
            backend="codex",
            project_mcp=True,
            allowed_tools=["mcp__docs__read_*"],
        )


def test_codex_project_mcp_forwards_declared_servers(tmp_path):
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text(
        '[mcp_servers.basic-memory]\nurl = "http://localhost:10888/mcp"\n'
        '[mcp_servers.other]\ncommand = "x"\nenv = {A = "b"}\n'
    )
    profile = Profile(
        cwd=tmp_path,
        backend="codex",
        project_mcp=True,
        allowed_tools=["mcp__basic-memory*", "mcp__other__ping"],
    )
    command = codex_command(profile, None)
    overrides = [v for f, v in pairwise(command) if f == "-c"]
    memory = next(v for v in overrides if v.startswith("mcp_servers.basic-memory="))
    other = next(v for v in overrides if v.startswith("mcp_servers.other="))
    assert '"url" = "http://localhost:10888/mcp"' in memory
    assert "enabled_tools" not in memory
    assert '"default_tools_approval_mode" = "approve"' in memory
    assert '"enabled_tools" = ["ping"]' in other
    assert '"env" = {"A" = "b"}' in other
    # Undeclared servers fail loudly instead of silently exposing nothing.
    missing = Profile(cwd=tmp_path, backend="codex", project_mcp=True, allowed_tools=["mcp__nope"])
    with pytest.raises(ValueError, match="nope"):
        codex_command(missing, None)
    # Without the opt-in nothing from the folder is forwarded.
    plain = codex_command(Profile(cwd=tmp_path, backend="codex", allowed_tools=[]), None)
    assert not any("basic-memory" in part for part in plain)


async def test_coding_tools_and_specifiers(tmp_path, monkeypatch):
    profile = Profile(cwd=tmp_path, allowed_tools=["Read", "Edit", "Bash(git diff:*)"])
    client = mock_client(monkeypatch, [result()])
    [e async for e in ClaudeBackend().stream(profile, "p", None)]
    assert client.options.tools == ["Bash", "Edit", "Read"]
    assert "Bash(git diff:*)" in client.options.allowed_tools
    hook = client.options.hooks["PreToolUse"][0].hooks[0]
    for name in ("Edit", "Bash"):
        assert await hook({"tool_name": name}, None, None) == {}
    for name in ("Write", "WebFetch"):
        assert "hookSpecificOutput" in await hook({"tool_name": name}, None, None)
