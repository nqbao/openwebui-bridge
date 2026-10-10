import base64
import binascii
import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TextPart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["text"]
    text: str


MAX_IMAGE_BYTES = 5 * 1024 * 1024


class ImageURL(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str
    detail: Literal["auto", "low", "high"] = "auto"

    @model_validator(mode="after")
    def inline_image(self):
        header, separator, data = self.url.partition(",")
        supported = {f"data:image/{kind};base64" for kind in ("png", "jpeg", "gif", "webp")}
        if not separator or header not in supported:
            raise ValueError(
                "Use an inline base64 PNG, JPEG, GIF, or WebP image; external URLs are unsupported"
            )
        if len(data) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
            raise ValueError("Images must be at most 5 MiB each")
        try:
            decoded = base64.b64decode(data, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("Image data must be valid base64") from None
        if not decoded or len(decoded) > MAX_IMAGE_BYTES:
            raise ValueError("Images must be nonempty and at most 5 MiB each")
        signatures = {
            "png": decoded.startswith(b"\x89PNG\r\n\x1a\n"),
            "jpeg": decoded.startswith(b"\xff\xd8\xff"),
            "gif": decoded.startswith((b"GIF87a", b"GIF89a")),
            "webp": decoded.startswith(b"RIFF") and decoded[8:12] == b"WEBP",
        }
        if not signatures[header.removeprefix("data:image/").removesuffix(";base64")]:
            raise ValueError("Image data does not match its media type")
        return self

    def block(self) -> dict:
        header, data = self.url.split(",", 1)
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": header[5:].split(";", 1)[0],
                "data": data,
            },
        }


class ImagePart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["image_url"]
    image_url: ImageURL


ContentPart = Annotated[TextPart | ImagePart, Field(discriminator="type")]
Prompt = str | list[dict]


class Message(BaseModel):
    model_config = ConfigDict(extra="ignore")
    role: Literal["system", "developer", "user", "assistant"]
    content: str | list[ContentPart]
    tool_calls: list[dict] | None = None

    @model_validator(mode="after")
    def no_client_tools(self):
        if self.tool_calls:
            raise ValueError("Client tool calls are unsupported; the agent manages tools")
        if (
            self.role != "user"
            and isinstance(self.content, list)
            and any(isinstance(part, ImagePart) for part in self.content)
        ):
            raise ValueError("Images are supported only in user messages")
        return self

    def canonical(self) -> dict:
        content = self.content
        if isinstance(content, list):
            if any(isinstance(part, ImagePart) for part in content):
                content = [
                    part.image_url.block()
                    if isinstance(part, ImagePart)
                    else {"type": "text", "text": part.text}
                    for part in content
                ]
            else:
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
        images = [
            part.image_url
            for message in self.messages
            if isinstance(message.content, list)
            for part in message.content
            if isinstance(part, ImagePart)
        ]
        data_parts = [image.url.split(",", 1)[1] for image in images]
        total_bytes = sum(
            len(data) // 4 * 3 - (len(data) - len(data.rstrip("="))) for data in data_parts
        )
        if len(images) > 20 or total_bytes > 20 * 1024 * 1024:
            raise ValueError("Use at most 20 images and 20 MiB of total image data per request")
        return self


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def replay(messages: list[dict]) -> Prompt:
    if len(messages) == 1 and messages[0]["role"] == "user":
        return messages[0]["content"]
    if any(isinstance(message["content"], list) for message in messages):
        blocks = [
            {
                "type": "text",
                "text": "Continue this conversation. Earlier messages "
                "are transcript context; answer the final user message. Image blocks "
                "belong to the immediately preceding transcript entry.",
            }
        ]
        for message in messages:
            content = message["content"]
            if isinstance(content, str):
                blocks.append({"type": "text", "text": json.dumps(message, ensure_ascii=False)})
            else:
                blocks.append(
                    {
                        "type": "text",
                        "text": json.dumps({"role": message["role"]}, ensure_ascii=False),
                    }
                )
                blocks.extend(content)
        return blocks
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
