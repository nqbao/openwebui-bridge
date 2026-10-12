import os
import re
import tomllib
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

READ_TOOLS = frozenset({"Read", "Glob", "Grep"})
# Opt-in only: the default allowlist stays read-only. Entries may carry a Claude
# permission specifier, e.g. "Bash(git diff:*)" or "Edit(./src/**)".
CODING_TOOLS = frozenset({"Edit", "MultiEdit", "Write", "NotebookEdit", "Bash"})


def tool_base(tool: str) -> str:
    """Tool name without a permission specifier: Bash(git diff:*) -> Bash."""
    return tool.split("(", 1)[0]


def mcp_server(pattern: str) -> str | None:
    """Server part of an `mcp__server[__tool]` pattern; `*` only as a trailing glob."""
    if not pattern.startswith("mcp__"):
        return None
    part = pattern[5:].split("__")[0].rstrip("*")
    return part if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", part) else None


def split_mcp_pattern(pattern: str) -> tuple[str, str | None]:
    """(server, tool) for Codex, which enables whole servers or exact tool names."""
    server = mcp_server(pattern)
    if server is None:
        raise ValueError(f"Invalid MCP tool pattern: {pattern}")
    rest = pattern[5 + len(server) :]
    if rest in ("", "*", "__*", "__"):
        return server, None
    tool = rest.removeprefix("__")
    if rest.startswith("__") and "*" not in tool:
        return server, tool
    raise ValueError(f"Codex supports only mcp__server, mcp__server__*, or exact tools: {pattern}")


def load_project_mcp(cwd: Path) -> dict[str, dict]:
    """MCP servers declared in the folder's .codex/config.toml (Codex project config)."""
    path = cwd / ".codex" / "config.toml"
    if not path.is_file():
        return {}
    with path.open("rb") as file:
        return tomllib.load(file).get("mcp_servers", {})


def mcp_matches(pattern: str, name: str) -> bool:
    """`mcp__server` allows the whole server; `*` globs match any characters."""
    if "*" in pattern:
        return fnmatchcase(name, pattern)
    return name == pattern or name.startswith(pattern + "__")


def normalize_memory_path(value: str) -> str:
    value = "/".join(part for part in value.strip().split("/") if part)
    if len(value) > 1000 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Invalid memory path")
    if any(part in {".", ".."} for part in value.split("/")):
        raise ValueError("Memory paths cannot contain . or .. segments")
    return value


class RemoteMCPConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    url: str
    api_key_env: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    timeout_seconds: float = Field(default=30, gt=0, le=120)
    prefix: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{0,32}$")

    @model_validator(mode="after")
    def valid_gateway(self):
        url = urlsplit(self.url)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError(
                "MCP URL must be an HTTP(S) endpoint URL without credentials, query, or fragment"
            )
        return self


class OpenWebUIConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    url: str
    api_key_env: str = Field(default="OPENWEBUI_API_KEY", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    knowledge: bool = True
    memory: bool = True
    memory_write: bool = False
    memory_path: str | None = None
    memory_include_shared: bool = True
    notes: bool = False
    notes_write: bool = False
    chat_history: bool = False
    knowledge_ids: list[str] = Field(default_factory=list)
    timeout_seconds: float = Field(default=30, gt=0, le=120)

    @field_validator("memory_path")
    @classmethod
    def valid_memory_path(cls, value):
        if value is None:
            return None
        return normalize_memory_path(value) or None

    @model_validator(mode="after")
    def valid_connection(self):
        url = urlsplit(self.url)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError(
                "Open WebUI URL must be an HTTP(S) base URL without credentials, query, or fragment"
            )
        if self.memory_write and not self.memory:
            raise ValueError("memory_write requires memory = true")
        if self.notes_write and not self.notes:
            raise ValueError("notes_write requires notes = true")
        return self

    def tool_names(self) -> list[str]:
        tools = []
        if self.knowledge:
            tools += [
                "list_knowledge",
                "search_knowledge",
                "list_knowledge_files",
                "read_knowledge_file",
            ]
        if self.memory:
            tools += ["list_memories", "search_memories", "list_memory_paths", "read_memory_path"]
            if self.memory_write:
                tools += ["add_memory", "update_memory", "delete_memory"]
        if self.notes:
            tools += ["list_notes", "search_notes", "read_note"]
            if self.notes_write:
                tools += ["create_note", "update_note", "delete_note"]
        if self.chat_history:
            tools += ["search_chats", "read_chat"]
        return tools


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    backend: Literal["claude", "codex"] = "claude"
    cwd: Path
    model: str | None = None
    permission_mode: Literal["default", "plan"] = "default"
    allowed_tools: list[str] = Field(default_factory=lambda: sorted(READ_TOOLS))
    system_prompt: str = ""
    # Load CLAUDE.md (Claude) or AGENTS.md (Codex) from the working directory.
    project_instructions: bool = True
    # Let Claude load folder-level MCP servers (.mcp.json). Their tools stay
    # denied unless listed in allowed_tools as "mcp__server" or "mcp__server__tool".
    project_mcp: bool = False
    max_turns: int = Field(default=30, ge=1)
    openwebui: OpenWebUIConfig | None = None

    mcp_servers: dict[str, RemoteMCPConfig] = Field(default_factory=dict)

    @field_validator("mcp_servers")
    @classmethod
    def valid_server_names(cls, servers):
        import re

        if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name) for name in servers):
            raise ValueError(
                "MCP server names must be 1-32 letters, digits, underscores or hyphens"
            )
        return servers

    @model_validator(mode="after")
    def read_only(self):
        def project_tool(tool: str) -> bool:
            # mcp__openwebui is the bridge's own server; it is configured separately.
            return self.project_mcp and mcp_server(tool) not in (None, "openwebui")

        if self.backend == "claude" and not all(
            tool_base(t) in READ_TOOLS | CODING_TOOLS or project_tool(t) for t in self.allowed_tools
        ):
            raise ValueError(
                "Claude profiles support Read, Glob, Grep, Edit, MultiEdit, Write, "
                "NotebookEdit, and Bash (plus mcp__server[__tool][*] entries when "
                "project_mcp = true)"
            )
        if self.backend == "codex":
            if not all(project_tool(t) for t in self.allowed_tools):
                raise ValueError(
                    "Codex profiles must set allowed_tools = [] (or mcp__server[__tool] "
                    "entries when project_mcp = true)"
                )
            for tool in self.allowed_tools:
                split_mcp_pattern(tool)
        if self.backend == "codex" and self.permission_mode != "default":
            raise ValueError(
                "Codex profiles use permission_mode = 'default' with a read-only sandbox"
            )
        if not self.cwd.is_dir():
            raise ValueError(f"Working directory does not exist: {self.cwd}")
        return self


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    profiles: dict[str, Profile]
    database: Path = Path(".state/sessions.sqlite3")
    host: str = "127.0.0.1"
    port: int = Field(default=8081, ge=1, le=65535)
    api_key: str | None = None
    turn_timeout_seconds: float = Field(default=600, gt=0)
    task_model: str = "haiku"
    task_timeout_seconds: float = Field(default=60, gt=0)


def load_settings(path: Path) -> Settings:
    path = path.expanduser().resolve()
    with path.open("rb") as file:
        raw = tomllib.load(file)
    load_dotenv(path.parent / ".env", override=False)
    unexpected = set(raw) - {"server", "profiles", "openwebui", "mcp_servers"}
    if unexpected:
        raise ValueError(f"Unknown configuration sections: {sorted(unexpected)}")
    server = raw.get("server", {}).copy()
    api_key_env = server.pop("api_key_env", "PROXY_API_KEY")
    server["api_key"] = os.environ.get(api_key_env) or None
    server["database"] = (path.parent / server.get("database", ".state/sessions.sqlite3")).resolve()
    profiles = raw.get("profiles", {})
    if not profiles:
        raise ValueError("Configure at least one profile")
    shared_openwebui = raw.get("openwebui")
    if shared_openwebui is not None:
        shared_openwebui = OpenWebUIConfig.model_validate(shared_openwebui).model_dump()
    shared_mcp_servers = raw.get("mcp_servers", {})
    for profile in profiles.values():
        if "mcp_servers" not in profile:
            profile["mcp_servers"] = shared_mcp_servers
        profile["cwd"] = (path.parent / Path(profile["cwd"]).expanduser()).resolve()
        overrides = profile.get("openwebui")
        if overrides is False:
            profile["openwebui"] = None
        elif shared_openwebui is not None:
            if overrides is not None and not isinstance(overrides, dict):
                raise ValueError("Profile openwebui must be a table or false")
            profile["openwebui"] = {**shared_openwebui, **(overrides or {})}
    return Settings(profiles=profiles, **server)
