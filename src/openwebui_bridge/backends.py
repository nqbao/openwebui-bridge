import asyncio
import base64
import hashlib
import json
import os
import re
import signal
import tempfile
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path

from .config import (
    Profile,
    load_project_mcp,
    mcp_matches,
    mcp_server,
    split_mcp_pattern,
    tool_base,
)
from .images import MAX_EVENT_BYTES, ImageDeliveryError, decode_image
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
class Image:
    data: bytes
    mime_type: str
    alt: str | None = None


@dataclass(frozen=True)
class Done:
    session_id: str
    usage: dict | None = None


class CodexImages:
    """Process completed results independently of tool activity deduplication."""

    def __init__(self):
        self.seen: set[bytes] = set()
        self.total_bytes = 0

    def parse(self, item: dict) -> list[Image]:
        blocks = []
        if item.get("type") == "Extension" and item.get("kind") == "image_gen.generation":
            if item.get("status") != "completed":
                return []
            if item.get("failure") or not item.get("result"):
                raise ImageDeliveryError("Image delivery failed: Codex returned no image payload")
            blocks = [{"type": "image", "data": item["result"]}]
        elif item.get("type") in {"image_generation", "image_generation_call"}:
            if not item.get("result"):
                raise ImageDeliveryError("Image delivery failed: Codex returned no image payload")
            blocks = [{"type": "image", "data": item["result"]}]
        elif item.get("type") == "mcp_tool_call":
            result = item.get("result") or {}
            if not isinstance(result, dict) or result.get("isError"):
                return []
            blocks = result.get("content") or []
        images = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") in {"resource", "resource_link"}:
                resource = block.get("resource", block)
                if str(resource.get("mimeType", "")).startswith("image/"):
                    if "blob" not in resource:
                        continue  # Reference-only image; nothing to deliver.
                    block = {
                        "type": "image",
                        "data": resource["blob"],
                        "mimeType": resource.get("mimeType"),
                    }
            if block.get("type") != "image":
                continue
            data, mime_type = decode_image(block.get("data"), block.get("mimeType"))
            identity = hashlib.sha256(mime_type.encode() + data).digest()
            if identity in self.seen:
                continue
            if len(self.seen) >= 20 or self.total_bytes + len(data) > 40 * 1024 * 1024:
                raise ImageDeliveryError("Image delivery failed: turn image limit exceeded")
            self.seen.add(identity)
            self.total_bytes += len(data)
            images.append(Image(data, mime_type))
        return images


class CodexImageLog:
    """Compatibility supplement for exec JSONL omitting image_gen Extension items.

    Read only this invocation's completed native items. Never open savedPath or
    model-supplied paths, and never replay images from previous resumed turns.
    """

    def __init__(self, resume: str | None):
        self.root = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "sessions"
        self.path = self.locate(resume) if resume else None
        self.offset = self.path.stat().st_size if self.path else 0
        self.turn_id = None

    def locate(self, session_id: str) -> Path | None:
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", session_id):
            return None
        for path in self.root.glob(f"*/*/*/rollout-*-{session_id}.jsonl"):
            if path.is_file() and path.resolve().is_relative_to(self.root.resolve()):
                return path
        return None

    def read(self, session_id: str | None, until_text: str | None = None) -> list[dict]:
        if not session_id:
            return []
        if self.path is None:
            self.path = self.locate(session_id)
        if self.path is None:
            return []
        items = []
        scanned = 0
        with self.path.open("rb") as log:
            log.seek(self.offset)
            while line := log.readline(MAX_EVENT_BYTES + 1):
                scanned += len(line)
                if scanned > 64 * 1024 * 1024:
                    raise ImageDeliveryError("Image delivery failed: Codex log scan limit exceeded")
                if len(line) > MAX_EVENT_BYTES:
                    raise ImageDeliveryError(
                        "Image delivery failed: Codex result exceeds event limit"
                    )
                if not line.endswith(b"\n"):
                    break  # A concurrent writer has not finished this record yet.
                self.offset = log.tell()
                record = json.loads(line)
                if record.get("type") != "event_msg":
                    continue
                payload = record.get("payload", {})
                if payload.get("type") == "task_started":
                    self.turn_id = payload.get("turn_id")
                elif (
                    self.turn_id
                    and payload.get("type") == "item_completed"
                    and payload.get("thread_id") == session_id
                    and payload.get("turn_id") == self.turn_id
                ):
                    item = payload.get("item", {})
                    if item.get("type") == "Extension":
                        items.append(item)
                    elif until_text is not None and item.get("type") == "AgentMessage":
                        text = "".join(
                            part.get("text", "")
                            for part in item.get("content", [])
                            if part.get("type") == "Text"
                        )
                        if text == until_text:
                            break
        return items


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
            # Specifiers such as Bash(git diff:*) are enforced by the CLI's own
            # permission rules; unmatched calls reach deny_approval below.
            if name not in {tool_base(t) for t in allowed_tools} and not (
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
            tools=sorted(
                {tool_base(t) for t in profile.allowed_tools if not t.startswith("mcp__")}
            ),
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
        image_log = CodexImageLog(resume) if profile.codex_image_log else None
        image_results = CodexImages()
        process = await asyncio.create_subprocess_exec(
            *codex_command(profile, resume, images, gateway_tools),
            cwd=profile.cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            limit=MAX_EVENT_BYTES,
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
                message = event.get("item")
                if not isinstance(message, dict):
                    message = {}
                text_boundary = kind == "item.completed" and message.get("type") == "agent_message"
                if image_log and (text_boundary or kind == "turn.completed"):
                    for item in image_log.read(
                        session_id, (message.get("text") or "") if text_boundary else None
                    ):
                        for image in image_results.parse(item):
                            yield image
                if kind == "thread.started":
                    continue
                elif kind == "turn.completed":
                    completed = True
                    usage = event.get("usage")
                elif kind in {"turn.failed", "error"}:
                    raise RuntimeError("Codex reported a failed turn")
                elif kind in {"item.started", "item.completed"}:
                    item = message
                    item_id = item.get("id")
                    item_type = item.get("type")
                    if kind == "item.completed":
                        for image in image_results.parse(item):
                            yield image
                    if item_type == "agent_message" and kind == "item.completed":
                        yield Text(item.get("text") or "")
                    elif (
                        item_type
                        in {"command_execution", "mcp_tool_call", "web_search", "file_change"}
                        and item_id not in seen
                    ):
                        seen.add(item_id)
                        yield Text(tool_line(item_type, item))
            await writer
            code = await process.wait()
            if image_log:
                for item in image_log.read(session_id):
                    for image in image_results.parse(item):
                        yield image
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
