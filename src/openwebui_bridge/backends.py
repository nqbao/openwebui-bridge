import asyncio
import base64
import json
import os
import signal
import tempfile
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path

from .config import Profile, load_project_mcp, mcp_matches, mcp_server, split_mcp_pattern
from .openwebui_mcp import (
    CONFIG_ENV,
    MCP_CONFIG_ENV,
    MCP_TOKENS_ENV,
    TOKEN_ENV,
    build_instructions,
    connection_env,
    discover_upstream_tools,
    server_command,
)
from .protocol import Prompt, tool_line


@dataclass(frozen=True)
class Text:
    content: str


@dataclass(frozen=True)
class Done:
    session_id: str
    usage: dict | None = None


class ClaudeBackend:
    async def stream(self, profile: Profile, prompt: Prompt, resume: str | None) -> AsyncIterator:
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

        integration = profile.openwebui
        mcp_tools = (
            [f"mcp__openwebui__{name}" for name in integration.tool_names()] if integration else []
        )
        gateway_tools = await discover_upstream_tools(profile.mcp_servers)
        mcp_tools += [f"mcp__openwebui__{name}" for name in gateway_tools]
        # The SDK sees server-level rules; the hook below enforces exact patterns.
        project_tools = [t for t in profile.allowed_tools if t.startswith("mcp__")]
        server_rules = sorted({f"mcp__{mcp_server(t)}" for t in project_tools})
        allowed_tools = [t for t in profile.allowed_tools if t not in project_tools]
        allowed_tools += mcp_tools + server_rules
        environment = connection_env(integration, profile.mcp_servers)
        system_prompt = profile.system_prompt
        if mcp_tools:
            system_prompt += "\n\n" + build_instructions(integration, bool(gateway_tools))

        async def deny_approval(_name, _inputs, _context):
            return PermissionResultDeny(message="This proxy has no interactive approval UI")

        async def restrict_tools(data, _tool_use_id, _context):
            name = data.get("tool_name")
            if name not in allowed_tools and not (
                not name.startswith("mcp__openwebui__")
                and any(mcp_matches(tool, name) for tool in project_tools)
            ):
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
            tools=[t for t in profile.allowed_tools if not t.startswith("mcp__")],
            allowed_tools=allowed_tools,
            permission_mode=profile.permission_mode,
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": system_prompt,
            }
            if profile.allowed_tools
            else system_prompt,
            mcp_servers={"openwebui": {"type": "stdio", **server_command()}} if mcp_tools else {},
            env=environment,
            # "local" is where `claude mcp add` stores per-directory servers
            # (~/.claude.json) and without it they are not discovered.
            setting_sources=["project", "local"]
            if profile.project_mcp
            else ["project"]
            if profile.project_instructions
            else [],
            # Ignore MCP servers from host/project config, including on resume,
            # unless the profile opts in to folder-level servers.
            strict_mcp_config=not profile.project_mcp,
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
            if isinstance(prompt, str):
                await client.query(prompt)
            else:

                async def user_input():
                    yield {
                        "type": "user",
                        "message": {"role": "user", "content": prompt},
                        "parent_tool_use_id": None,
                    }

                await client.query(user_input())
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


def toml_value(value) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, dict):
        items = ", ".join(f"{json.dumps(str(k))} = {toml_value(v)}" for k, v in value.items())
        return "{" + items + "}"
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    return json.dumps(value) if isinstance(value, str) else str(value)


def project_mcp_overrides(profile: Profile) -> list[str]:
    """Forward allowed servers from the folder's .codex/config.toml as inline config.

    Codex only reads project config for trusted projects, and trust lives in the
    user config that --ignore-user-config skips, so the bridge passes them itself.
    """
    wanted: dict[str, set[str] | None] = {}
    for pattern in profile.allowed_tools:
        server, tool = split_mcp_pattern(pattern)
        if tool is None:
            wanted[server] = None
        elif wanted.get(server, set()) is not None:
            wanted.setdefault(server, set()).add(tool)
    declared = load_project_mcp(profile.cwd)
    command = []
    for server, tools in wanted.items():
        if server not in declared:
            raise ValueError(f"MCP server {server!r} is not in {profile.cwd}/.codex/config.toml")
        config = {**declared[server], "required": False, "default_tools_approval_mode": "approve"}
        config.pop("enabled_tools", None)
        if tools is not None:
            config["enabled_tools"] = sorted(tools)
        command += ["-c", f"mcp_servers.{server}=" + toml_value(config)]
    return command


def codex_command(
    profile: Profile,
    resume: str | None,
    images: list[str] | None = None,
    gateway_tools: list[str] | None = None,
) -> list[str]:
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
    if not profile.project_instructions:
        command += ["-c", "project_doc_max_bytes=0"]  # Skip AGENTS.md.
    if profile.project_mcp:
        command += project_mcp_overrides(profile)
    system_prompt = profile.system_prompt
    if (profile.openwebui and profile.openwebui.tool_names()) or profile.mcp_servers:
        config = {
            **server_command(),
            "env_vars": [CONFIG_ENV, TOKEN_ENV]
            + ([MCP_CONFIG_ENV, MCP_TOKENS_ENV] if profile.mcp_servers else []),
            "enabled_tools": (profile.openwebui.tool_names() if profile.openwebui else [])
            + (gateway_tools or []),
            "required": True,
            "startup_timeout_sec": 20
            + sum(upstream.timeout_seconds for upstream in profile.mcp_servers.values()),
            "tool_timeout_sec": max(
                profile.openwebui.timeout_seconds if profile.openwebui else 0,
                max(
                    (upstream.timeout_seconds for upstream in profile.mcp_servers.values()),
                    default=0,
                ),
            )
            + 10,
        }
        # JSON string quoting is TOML-compatible; values contain no credentials.
        table = (
            "{"
            + ", ".join(
                f"{key} = {str(value).lower() if isinstance(value, bool) else json.dumps(value)}"
                for key, value in config.items()
            )
            + "}"
        )
        command += ["-c", "mcp_servers.openwebui=" + table]
        for name in gateway_tools or []:
            command += ["-c", f'mcp_servers.openwebui.tools.{name}.approval_mode="approve"']
        if profile.openwebui and profile.openwebui.memory_write:
            for tool in ("add_memory", "update_memory", "delete_memory"):
                command += ["-c", f'mcp_servers.openwebui.tools.{tool}.approval_mode="approve"']
        if profile.openwebui and profile.openwebui.notes_write:
            for tool in ("create_note", "update_note", "delete_note"):
                command += ["-c", f'mcp_servers.openwebui.tools.{tool}.approval_mode="approve"']
        system_prompt += "\n\n" + build_instructions(profile.openwebui, bool(gateway_tools))
    if system_prompt:
        command += ["-c", "developer_instructions=" + json.dumps(system_prompt)]
    if resume:
        command += ["resume", resume]
    if images:
        # Image flags belong to the resume subcommand on continued turns. The
        # separator prevents exec's variadic image option consuming stdin '-'.
        command += [f"--image={path}" for path in images]
        return command + ["--", "-"]
    return command + ["-"]


class CodexBackend:
    async def stream(self, profile: Profile, prompt: Prompt, resume: str | None) -> AsyncIterator:
        # Keep attachments outside the repository, private to this invocation.
        # The directory outlives the subprocess and is removed on all outcomes.
        with tempfile.TemporaryDirectory(prefix="openwebui-bridge-images-") as directory:
            images = []
            if isinstance(prompt, list):
                text = []
                for block in prompt:
                    if block["type"] == "text":
                        text.append(block["text"])
                    elif block["type"] == "image":
                        source = block["source"]
                        extension = source["media_type"].split("/", 1)[1]
                        path = Path(directory) / f"image-{len(images) + 1}.{extension}"
                        with path.open("xb") as file:
                            os.chmod(path, 0o600)
                            file.write(base64.b64decode(source["data"], validate=True))
                        images.append(str(path))
                        text.append(f"[Attached image {len(images)}]")
                prompt = "\n\n".join(text)
            async with aclosing(self._stream(profile, prompt, resume, images)) as events:
                async for event in events:
                    yield event

    async def _stream(
        self, profile: Profile, prompt: str, resume: str | None, images: list[str]
    ) -> AsyncIterator:
        gateway_tools = await discover_upstream_tools(profile.mcp_servers)
        process = await asyncio.create_subprocess_exec(
            *codex_command(profile, resume, images, gateway_tools),
            cwd=profile.cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            limit=4 * 1024 * 1024,
            env={**os.environ, **connection_env(profile.openwebui, profile.mcp_servers)},
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
