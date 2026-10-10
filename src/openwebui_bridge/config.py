import os
import tomllib
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, model_validator

READ_TOOLS = frozenset({"Read", "Glob", "Grep"})


class OpenWebUIConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    url: str
    api_key_env: str = Field(default="OPENWEBUI_API_KEY", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    knowledge: bool = True
    memory: bool = True
    memory_write: bool = False
    knowledge_ids: list[str] = Field(default_factory=list)
    timeout_seconds: float = Field(default=30, gt=0, le=120)

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
            tools += ["list_memories", "search_memories"]
            if self.memory_write:
                tools += ["add_memory", "update_memory", "delete_memory"]
        return tools


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    backend: Literal["claude", "codex"] = "claude"
    cwd: Path
    model: str | None = None
    permission_mode: Literal["default", "plan"] = "default"
    allowed_tools: list[str] = Field(default_factory=lambda: sorted(READ_TOOLS))
    system_prompt: str = ""
    max_turns: int = Field(default=30, ge=1)
    openwebui: OpenWebUIConfig | None = None

    @model_validator(mode="after")
    def read_only(self):
        if self.backend == "claude" and not set(self.allowed_tools) <= READ_TOOLS:
            raise ValueError("Claude profiles currently support only Read, Glob, and Grep")
        if self.backend == "codex" and self.allowed_tools:
            raise ValueError("Codex profiles must set allowed_tools = []")
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
    unexpected = set(raw) - {"server", "profiles", "openwebui"}
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
    for profile in profiles.values():
        profile["cwd"] = (path.parent / Path(profile["cwd"]).expanduser()).resolve()
        overrides = profile.get("openwebui")
        if overrides is False:
            profile["openwebui"] = None
        elif shared_openwebui is not None:
            if overrides is not None and not isinstance(overrides, dict):
                raise ValueError("Profile openwebui must be a table or false")
            profile["openwebui"] = {**shared_openwebui, **(overrides or {})}
    return Settings(profiles=profiles, **server)
