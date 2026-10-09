import asyncio
import os
import sys

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock
from claude_agent_sdk.types import StreamEvent

from openwebui_cli_proxy.backends import ClaudeBackend, CodexBackend, Done, Text, codex_command
from openwebui_cli_proxy.config import Profile
from openwebui_cli_proxy.protocol import tool_line


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
            Client.prompt = prompt

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
    assert client.options.setting_sources == []
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
        "openwebui_cli_proxy.backends.codex_command", lambda *_args: [sys.executable, str(script)]
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
        "openwebui_cli_proxy.backends.codex_command", lambda *_args: [sys.executable, str(script)]
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
        "openwebui_cli_proxy.backends.codex_command", lambda *_args: [sys.executable, "-c", code]
    )
    profile = Profile(cwd=tmp_path, backend="codex", allowed_tools=[])
    with pytest.raises(RuntimeError):
        _ = [e async for e in CodexBackend().stream(profile, "hello", None)]
