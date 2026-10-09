import argparse
import logging
from pathlib import Path

import uvicorn

from .app import create_app
from .config import load_settings


def main():
    parser = argparse.ArgumentParser(description="Host-local OpenAI-compatible agent proxy")
    parser.add_argument("--config", type=Path, default=Path("profiles.toml"))
    args = parser.parse_args()
    settings = load_settings(args.config)
    proxy_logger = logging.getLogger("openwebui_cli_proxy")
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    proxy_logger.addHandler(handler)
    proxy_logger.setLevel(logging.INFO)
    proxy_logger.propagate = False
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, workers=1)


if __name__ == "__main__":
    main()
