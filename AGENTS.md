# Working in this repository

- Use Python 3.11+ and `uv`. Install development dependencies with `uv sync --extra dev`.
- Run locally: `uv run openwebui-bridge --config profiles.toml`.
- Keep README instructions focused on how to use the bridge.
- Keep secrets in `.env` or environment variables. Use `profiles.example.toml` for portable configuration examples.

## Where to look

- `src/openwebui_bridge/app.py`: HTTP endpoints and streaming.
- `src/openwebui_bridge/backends.py`: Claude and Codex execution.
- `src/openwebui_bridge/config.py`: profiles and configuration validation.
- `src/openwebui_bridge/store.py`: session persistence.
- `src/openwebui_bridge/openwebui_mcp.py`: Open WebUI and remote MCP tools.
- `tests/`: automated tests and opt-in live smoke scripts.

## Before finishing

Run checks appropriate to the change. For Python changes:

```sh
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
```

For documentation-only changes, verify examples against the code and run `git diff --check`.
Live smoke scripts use authenticated agents; run them only when requested and
report live integration results separately from automated test results.
