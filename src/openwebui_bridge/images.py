"""Validated generated images."""

import base64
import binascii
import io
import warnings

from PIL import Image as PILImage

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_EVENT_BYTES = 16 * 1024 * 1024
IMAGE_TYPES = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
    "GIF": "image/gif",
    "MPO": "image/jpeg",  # Multi-picture JPEG, as written by some phones.
}


class ImageDeliveryError(RuntimeError):
    """Safe to display without leaking tool output, paths, or credentials."""


def validate_image(data: bytes, mime_type: str | None = None) -> str:
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ImageDeliveryError("Image delivery failed: image exceeds the 10 MiB payload limit")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", PILImage.DecompressionBombWarning)
            with PILImage.open(io.BytesIO(data)) as image:
                detected = IMAGE_TYPES.get(image.format)
                if not detected or image.width * image.height > 32_000_000:
                    raise ValueError("Unsupported image")
                image.verify()
        if mime_type is not None and detected != mime_type.strip().lower():
            raise ValueError("MIME mismatch")
    except (
        OSError,
        ValueError,
        SyntaxError,
        PILImage.DecompressionBombError,
        PILImage.DecompressionBombWarning,
    ):
        raise ImageDeliveryError(
            "Image delivery failed: invalid or unsupported image data"
        ) from None
    return detected


def decode_image(value: str, mime_type: str | None = None) -> tuple[bytes, str]:
    if not isinstance(value, str) or len(value) > 4 * ((MAX_IMAGE_BYTES + 2) // 3) + 100:
        raise ImageDeliveryError("Image delivery failed: image exceeds the 10 MiB payload limit")
    if value.startswith("data:"):
        header, separator, value = value.partition(",")
        if not separator or not header.endswith(";base64"):
            raise ImageDeliveryError("Image delivery failed: unsupported image encoding")
        # Ignore case and parameters such as ;charset=utf-8.
        encoded_type = header[5:-7].split(";")[0].strip().lower()
        if mime_type is not None and mime_type.strip().lower() != encoded_type:
            raise ImageDeliveryError("Image delivery failed: image MIME types disagree")
        mime_type = encoded_type
    try:
        data = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise ImageDeliveryError("Image delivery failed: invalid base64 image data") from None
    return data, validate_image(data, mime_type)
