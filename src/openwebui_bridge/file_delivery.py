"""Files explicitly sent by an agent during the current bridge turn."""

import os
import re
import stat
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

DOWNLOADS_ENV = "OPENWEBUI_BRIDGE_DOWNLOADS"
MAX_FILE_BYTES = 10 * 1024 * 1024
_downloads: ContextVar[Path | None] = ContextVar("downloads", default=None)
_file_id = re.compile(r"[A-Za-z0-9_-]{1,200}")


def valid_filename(name: str) -> bool:
    return (
        0 < len(name) <= 255
        and not name.startswith(".")
        and name == name.strip()
        and all(char.isprintable() and char not in "/\\" for char in name)
    )


def read_file(directory: Path, name: str, limit: int) -> bytes:
    """Read a bounded regular file directly under the configured directory."""
    if not valid_filename(name):
        raise ValueError("Use a plain filename.")
    parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("Choose a regular file in the output directory.")
            if info.st_size > limit:
                raise ValueError("The file exceeds the size limit.")
            data = file.read(limit + 1)
            if len(data) > limit:
                raise ValueError("The file exceeds the size limit.")
            return data
    finally:
        os.close(parent)


def download_url(file_id: str) -> str:
    return f"/api/v1/files/{file_id}/content?attachment=true"


@contextmanager
def collect_downloads():
    with tempfile.TemporaryDirectory(prefix="openwebui-downloads-") as directory:
        path = Path(directory)
        token = _downloads.set(path)
        try:
            yield path
        finally:
            _downloads.reset(token)


def download_env() -> dict[str, str]:
    path = _downloads.get()
    return {DOWNLOADS_ENV: str(path)} if path is not None else {}


def record_download(file_id: str, filename: str) -> None:
    directory = os.environ.get(DOWNLOADS_ENV)
    if directory:
        try:
            with (Path(directory) / file_id).open("x", encoding="utf-8") as file:
                file.write(filename)
        except FileExistsError:
            pass


def download_links(directory: Path) -> list[tuple[str, str]]:
    links = []
    for path in sorted(directory.iterdir()):
        if not _file_id.fullmatch(path.name):
            continue
        try:
            filename = read_file(directory, path.name, 1024).decode()
        except (OSError, ValueError):
            continue
        if valid_filename(filename):
            label = re.sub(r"([\\`*_{}\[\]<>!|])", r"\\\1", filename)
            url = download_url(path.name)
            links.append((url, f"\n\n[Download {label}]({url})\n\n"))
    return links
