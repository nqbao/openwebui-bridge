import asyncio
import json
import os
import signal
from collections.abc import AsyncIterator
from dataclasses import dataclass

from .config import Profile
from .protocol import tool_line


@dataclass(frozen=True)
class Text:
    content: str


@dataclass(frozen=True)
class Done:
    session_id: str
    usage: dict | None = None


class ClaudeBackend:
    async def stream(self, profile: Profile, prompt: str, resume: str | None) -> AsyncIterator:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ClaudeSDKClient,
            HookMatcher,
            PermissionResultDeny,
            ResultMessage,
            TextBlock,
            ToolUseBlock,
        )
        from claude_agent_sdk.types import StreamEvent

        async def deny_approval(_name, _inputs, _context):
            return PermissionResultDeny(message="This proxy has no interactive approval UI")

        async def restrict_tools(data, _tool_use_id, _context):
            if data.get("tool_name") not in profile.allowed_tools:
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": "Tool is not enabled by the proxy profile",
                    }
                }
            return {}

        options = ClaudeAgentOptions(
            cwd=str(profile.cwd),
            model=profile.model,
            tools=profile.allowed_tools,
            allowed_tools=profile.allowed_tools,
            permission_mode=profile.permission_mode,
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": profile.system_prompt,
            }
            if profile.allowed_tools
            else profile.system_prompt,
            setting_sources=[],
            # Ignore MCP servers from host/project config, including on resume.
            strict_mcp_config=True,
            verbatim_prompts=True,
            hooks={"PreToolUse": [HookMatcher(hooks=[restrict_tools])]},
            can_use_tool=deny_approval,
            include_partial_messages=True,
            max_turns=profile.max_turns,
            resume=resume,
        )
        result = None
        streamed_message = False
        rendered_tools: set[str] = set()
        blocks: dict[int, dict] = {}
        # The caller runs this entire iterator in one producer task: SDK cancel
        # scopes must be entered/exited in the same task, even on HTTP disconnect.
        async with ClaudeSDKClient(options=options) as client:
            await client.query(prompt)
            async for message in client.receive_response():
                if isinstance(message, StreamEvent):
                    if message.parent_tool_use_id:
                        continue
                    event = message.event
                    kind = event.get("type")
                    if kind == "message_start":
                        streamed_message = False
                        blocks.clear()
                    elif kind == "content_block_start":
                        block = event.get("content_block", {})
                        if block.get("type") == "tool_use":
                            blocks[event["index"]] = {**block, "partial": ""}
                    elif kind == "content_block_delta":
                        delta = event.get("delta", {})
                        if delta.get("type") == "text_delta":
                            streamed_message = True
                            yield Text(delta.get("text", ""))
                        elif delta.get("type") == "input_json_delta":
                            block = blocks.get(event.get("index"))
                            if block is not None:
                                block["partial"] += delta.get("partial_json", "")
                    elif kind == "content_block_stop":
                        block = blocks.pop(event.get("index"), None)
                        if block and block["id"] not in rendered_tools:
                            try:
                                inputs = (
                                    json.loads(block["partial"])
                                    if block["partial"]
                                    else block.get("input", {})
                                )
                            except json.JSONDecodeError:
                                inputs = {}
                            rendered_tools.add(block["id"])
                            yield Text(tool_line(block["name"], inputs))
                elif isinstance(message, AssistantMessage):
                    if message.parent_tool_use_id:
                        continue
                    if message.error:
                        raise RuntimeError(f"Claude assistant error: {message.error}")
                    for block in message.content:
                        if isinstance(block, TextBlock) and not streamed_message:
                            yield Text(block.text)
                        elif isinstance(block, ToolUseBlock) and block.id not in rendered_tools:
                            rendered_tools.add(block.id)
                            yield Text(tool_line(block.name, block.input))
                    streamed_message = False
                elif isinstance(message, ResultMessage):
                    if message.is_error or message.subtype != "success":
                        raise RuntimeError(f"Claude turn failed: {message.subtype}")
                    result = Done(message.session_id, message.usage)
        if result is None or not result.session_id:
            raise RuntimeError("Claude stream ended without a successful result")
        yield result


def codex_command(profile: Profile, resume: str | None) -> list[str]:
    # Put parent exec flags before resume: the resume subcommand has fewer flags.
    command = [
        "codex",
        "exec",
        "--json",
        "--color",
        "never",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "-c",
        'approval_policy="never"',
        "--ignore-user-config",
        "--ignore-rules",
    ]
    if profile.model:
        command += ["--model", profile.model]
    if profile.system_prompt:
        command += ["-c", "developer_instructions=" + json.dumps(profile.system_prompt)]
    if resume:
        command += ["resume", resume]
    return command + ["-"]


class CodexBackend:
    async def stream(self, profile: Profile, prompt: str, resume: str | None) -> AsyncIterator:
        process = await asyncio.create_subprocess_exec(
            *codex_command(profile, resume),
            cwd=profile.cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            limit=4 * 1024 * 1024,
        )

        async def write_prompt():
            process.stdin.write(prompt.encode())
            await process.stdin.drain()
            process.stdin.close()
            await process.stdin.wait_closed()

        async def drain_stderr():
            # Drain continuously without retaining potentially sensitive output.
            while await process.stderr.read(65536):
                pass

        writer = asyncio.create_task(write_prompt())
        stderr = asyncio.create_task(drain_stderr())
        session_id = resume
        usage = None
        completed = False
        seen: set[str] = set()
        try:
            async for line in process.stdout:
                event = json.loads(line)
                kind = event.get("type")
                if kind == "thread.started":
                    session_id = event["thread_id"]
                elif kind == "turn.completed":
                    completed = True
                    usage = event.get("usage")
                elif kind in {"turn.failed", "error"}:
                    raise RuntimeError("Codex reported a failed turn")
                elif kind in {"item.started", "item.completed"}:
                    item = event.get("item", {})
                    item_id = item.get("id")
                    item_type = item.get("type")
                    if item_type == "agent_message" and kind == "item.completed":
                        yield Text(item.get("text", ""))
                    elif (
                        item_type
                        in {"command_execution", "mcp_tool_call", "web_search", "file_change"}
                        and item_id not in seen
                    ):
                        seen.add(item_id)
                        yield Text(tool_line(item_type, item))
            await writer
            code = await process.wait()
            if code != 0 or not completed or not session_id:
                raise RuntimeError("Codex exited without a successful completed turn")
        finally:
            # Kill the process group, including any tool descendants holding pipes.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except TimeoutError:
                pass
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
            for task in (writer, stderr):
                task.cancel()
            await asyncio.gather(writer, stderr, return_exceptions=True)
        yield Done(session_id, usage)
