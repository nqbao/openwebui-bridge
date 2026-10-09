import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

READ_TOOLS = frozenset({"Read", "Glob", "Grep"})


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    backend: Literal["claude", "codex"] = "claude"
    cwd: Path
    model: str | None = None
    permission_mode: Literal["default", "plan"] = "default"
    allowed_tools: list[str] = Field(default_factory=lambda: sorted(READ_TOOLS))
    system_prompt: str = ""
    max_turns: int = Field(default=30, ge=1)

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
    unexpected = set(raw) - {"server", "profiles"}
    if unexpected:
        raise ValueError(f"Unknown configuration sections: {sorted(unexpected)}")
    server = raw.get("server", {}).copy()
    api_key_env = server.pop("api_key_env", "PROXY_API_KEY")
    server["api_key"] = os.environ.get(api_key_env) or None
    server["database"] = (path.parent / server.get("database", ".state/sessions.sqlite3")).resolve()
    profiles = raw.get("profiles", {})
    if not profiles:
        raise ValueError("Configure at least one profile")
    for profile in profiles.values():
        profile["cwd"] = (path.parent / Path(profile["cwd"]).expanduser()).resolve()
    return Settings(profiles=profiles, **server)
