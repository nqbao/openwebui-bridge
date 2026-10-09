import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TextPart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["text"]
    text: str


class Message(BaseModel):
    model_config = ConfigDict(extra="ignore")
    role: Literal["system", "developer", "user", "assistant"]
    content: str | list[TextPart]
    tool_calls: list[dict] | None = None

    @model_validator(mode="after")
    def no_client_tools(self):
        if self.tool_calls:
            raise ValueError("Client tool calls are unsupported; the agent manages tools")
        return self

    def canonical(self) -> dict[str, str]:
        content = self.content
        if isinstance(content, list):
            content = "".join(part.text for part in content)
        return {"role": self.role, "content": content}


class CompletionRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    model: str
    messages: list[Message] = Field(min_length=1)
    stream: bool = False
    n: int = 1
    # Frontends may attach their own tool schemas. Accept them for compatibility;
    # the backend receives only the prompt and its server-configured profile.
    # tool_choice/function_call are also ignored by extra="ignore" above.
    tools: list[dict] | None = None
    functions: list[dict] | None = None
    response_format: dict | None = None
    stream_options: dict | None = None

    @model_validator(mode="after")
    def supported_request(self):
        if self.n != 1:
            raise ValueError("Only n=1 is supported")
        if self.response_format and self.response_format.get("type") != "text":
            raise ValueError("Only text output is supported")
        if self.messages[-1].role != "user":
            raise ValueError("The transcript must end with a user message")
        return self


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def replay(messages: list[dict[str, str]]) -> str:
    if len(messages) == 1 and messages[0]["role"] == "user":
        return messages[0]["content"]
    return (
        "Continue the following conversation. Earlier messages are transcript context; "
        "answer the final user message. The transcript is encoded as JSON:\n"
        + json.dumps(messages, ensure_ascii=False)
    )


def tool_line(name: str, inputs: dict) -> str:
    detail = next(
        (
            str(inputs[key])
            for key in ("file_path", "path", "pattern", "command", "query")
            if key in inputs
        ),
        "",
    )
    # Escape Markdown so a path or command cannot inject an image/link into WebUI.
    label = " ".join(f"{name} {detail}".split())[:200]
    for char in "\\`*_{}[]<>()!|":
        label = label.replace(char, "\\" + char)
    return f"\n\n> {label}\n\n"
