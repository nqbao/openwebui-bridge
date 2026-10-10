# Open WebUI Bridge

A host-local FastAPI server that exposes agent profiles as OpenAI models. Claude
runs through the Python Claude Agent SDK; an optional Codex backend runs
`codex exec --json`. Agent tools execute on the host and appear as short Markdown
lines in the assistant response. Open WebUI never executes those tools.

## Run

Requires macOS or Linux, Python 3.11+, `uv`, and agent authentication on the host. The Claude SDK
includes its CLI. Use `claude auth login` with a separately installed Claude CLI,
or supply `ANTHROPIC_API_KEY` to the server process. Codex profiles require the
`codex` executable on PATH and `codex login` or its supported API authentication.

```sh
uv sync --extra dev
cp profiles.example.toml profiles.toml
# Edit each profile's cwd to your repository's absolute path.
cp .env.example .env
# Edit .env with your keys.
uv run openwebui-bridge --config profiles.toml
```

The example listens on `127.0.0.1:8081`. Profiles and paths are loaded once at
startup. Relative paths resolve against the configuration file. The model ID
selects a profile; clients cannot override its directory, tools, or agent model.
Restart after changing configuration. Use one process/worker per SQLite database;
an exclusive file lock rejects a second process using the same database.

The bridge loads `.env` from the directory containing `profiles.toml` (or the file
passed to `--config`). Existing environment variables take precedence. The file
is optional and ignored by Git; `.env.example` lists example keys. Custom
`api_key_env` names also work in `.env`. Restart after changing it. The Open WebUI
connectivity check loads the same file. Loading `.env` does not export its values
into your shell; shell commands using `$PROXY_API_KEY` need that variable exported
separately.

## Connect Open WebUI

1. Set `ENABLE_FORWARD_USER_INFO_HEADERS=true` in Open WebUI's environment and
   restart it. Keep the default `X-OpenWebUI-Chat-Id` and
   `X-OpenWebUI-User-Id` header names. Use plain forwarded user headers rather than
   Open WebUI's optional JWT-only forwarding mode, which this proxy does not decode.
2. Add an **OpenAI connection** with URL `http://127.0.0.1:8081/v1` and the
   same API key as `PROXY_API_KEY`. Select `claude-code:myrepo` from the model list.
3. Background requests for titles, tags, and follow-ups can use this connection.
   They run through Claude SDK in fresh sessions with tools disabled, using
   `server.task_model` (default `haiku`). You can also choose a separate plain task
   model in Open WebUI. For explicitly marked streaming tasks, add the custom
   header `{"X-OpenWebUI-Task": "{{TASK}}"}` to the connection (Open WebUI 0.10.0+).
4. Keep streaming enabled. Open WebUI may attach tool definitions with native
   function calling enabled; the proxy accepts and ignores this metadata. Tool
   execution uses the agent profile, and progress appears as inline text.

If Open WebUI runs in Docker on macOS, use
`http://host.docker.internal:8081/v1`, and set the proxy's `server.host` to
`0.0.0.0` so the container can reach it. On Linux, arrange host gateway networking
or run Open WebUI with host networking. Set a proxy API key before exposing the
host listener; forwarded identity headers are trusted only from that connection.
All users of a profile share the host account and filesystem access. User headers
isolate session mappings; they do not provide separate filesystem permissions.

The current [Open WebUI header documentation](https://docs.openwebui.com/reference/env-configuration/)
describes forwarding chat IDs to OpenAI connections. The
[Claude Agent SDK reference](https://code.claude.com/docs/en/agent-sdk/python)
documents partial events and session resume; the
[official OpenAI documentation](https://developers.openai.com/codex/noninteractive/)
describes Codex JSON output and noninteractive resume.

## Request behavior

`GET /v1/models` returns one model per configured profile.
`POST /v1/chat/completions` requires a transcript ending in a user message.
With `stream=true`, responses use OpenAI SSE chunks, one completion ID, a final
`finish_reason: "stop"`, and `data: [DONE]`. Optional
`stream_options.include_usage` returns an additional usage chunk when available.
These are agent-turn totals, potentially including multiple model calls, not just
the final answer. With `stream=false` (the default), it returns a standard OpenAI
JSON completion containing `choices[0].message.content` and optional usage.
`/health` reports HTTP server health without invoking an agent.

Text strings and lists of text parts are supported. Claude and Codex profiles accept
inline images (see below). Audio, conversation history containing client tool
calls/results, assistant prefill, multiple choices, and structured response formats
return HTTP 400.

### Image attachments

In Open WebUI, edit the model under **Workspace → Models** and enable **Vision**
and **File Upload** in its capabilities. Select a Claude or Codex profile, attach a
screenshot, and send a question. These capabilities are described in the
[Open WebUI model documentation](https://docs.openwebui.com/features/workspace/models/).

The proxy accepts OpenAI `image_url` parts in user messages with inline
`data:image/png;base64,...` URLs. JPEG, GIF, and WebP are also supported. Images
are passed as structured Claude image blocks, including in isolated JSON tasks;
the SDK's structured-input interface is described in its
[streaming input guide](https://code.claude.com/docs/en/agent-sdk/streaming-vs-single-mode).
Codex receives private temporary image files through `codex exec --image`, or
`codex exec resume --image` on continued turns. Numbered image markers associate
the attachments with their places in the prompt. Files are removed after the
subprocess exits, including on failure or cancellation. CLI image flags are
documented in the [official OpenAI command reference](https://developers.openai.com/codex/cli/reference/).
Images are not encoded into prompt text or written to the repository. Native
agent session files can retain them. Image content participates in prefix hashing,
and transcript replay includes the original image blocks after edits or imports.

Limits: 5 MiB per image, 20 images and 20 MiB of decoded image data per request,
including history. The `detail` hint is accepted but does not change Claude input.
External image URLs and images in non-user messages return HTTP 400.
Original document uploads and file transfer are
separate from image input.

Frontend `tools`, `tool_choice`, legacy `functions`, and `function_call` fields are
accepted and ignored, including named/required tool choices. This follows the
agent-backend approach: the configured Claude/Codex runtime owns tool execution.
These fields do not enable Open WebUI's built-in or attached tools, alter the
profile's permissions, or affect session matching. The proxy emits no
`delta.tool_calls` for Open WebUI to execute.

Extra sampling fields (temperature, max_tokens, stop, etc.) are accepted but
ignored; agent behavior is controlled by the profile. Reasoning and subagent text
are not rendered. Claude text streams token by token. Codex JSON mode delivers
assistant text at completed-message boundaries and tool status as items arrive.

For ordinary streaming chat with a forwarded chat ID, each turn takes a lock scoped by user, chat, profile,
and configuration. SQLite has one `sessions` table with `chat_id`, `session_id`,
`cwd`, `prefix_hash`, and `updated_at`; `chat_id` stores the hash of this scope.

- A new chat starts a native agent session. If it already includes history, the
  transcript is replayed as JSON text so imported chats retain context.
- On the next turn, the hash of all messages before the final user message must
  match the saved hash. The proxy resumes the native session and sends only the
  new user content, including any new images.
- Edits, regeneration, branch changes, altered system messages, and changed
  profiles start a new session with the current transcript. This restores visible
  conversation context, including displayed tool lines, but does not recreate
  earlier hidden agent reasoning or tool results.
- Without a chat ID, every request starts a fresh session with the flattened
  transcript and writes no mapping to SQLite.

The saved prefix includes the newly submitted user message **and the exact
assistant content streamed back**, including tool lines. The next WebUI request
must preserve that text for resume to match. RAG injections, filters, truncation,
or whitespace changes can trigger a fresh replay. A stable WebUI system prompt
is carried in the initial transcript; profile instructions are appended to the
agent system prompt.

The mapping is deleted before starting the backend and saved only after a
successful result and backend cleanup. Failures, timeout, disconnect, or a process
crash leave no reusable mapping for a partially advanced native session. A failed
resume returns an SSE error; retry starts fresh from the supplied transcript.
An SSE error after HTTP headers have been sent retains HTTP 200 and ends with
`[DONE]`, without a successful finish chunk. Timeout defaults to 600 seconds;
SSE comments keep idle connections alive every 10 seconds.

### Diagnosing extra requests

Open WebUI can send background requests for titles, tags, and follow-ups to the
same model. These usually have `stream=false`. All non-streaming requests run as
isolated Claude SDK completions with no tools, no native session resume, and no
SQLite reads or writes. This also applies when the selected chat profile uses
Codex: tasks use Claude's SDK to enforce an empty tool set. Each task receives the
supplied transcript and standalone task instructions, without the coding profile's
system prompt. Tasks require host Claude authentication.

Configure `server.task_model` and `server.task_timeout_seconds` (defaults `haiku`
and 60 seconds). Task calls are additional model requests. A non-streaming backend
failure returns HTTP 502; a timeout returns HTTP 504. Disconnect cancels the task
and waits for backend cleanup. Normal streaming chats retain the configured tools
and native session resume.

Validation errors still log their stream flag and reason, for example:

```text
Rejected completion request: stream=False; task=unspecified; body: Value error, Only n=1 is supported
```

Validation runs before agent invocation, so these rejected requests do not resume,
delete, or overwrite the chat's session mapping. The log excludes prompts and
authorization headers. `stream=false` alone does not establish that a request is
a background task; an ordinary client can also request non-streaming output.

Open WebUI's internal `metadata.task` is removed from the default upstream OpenAI
payload. For explicit identification, versions supporting custom header templates
can forward `X-OpenWebUI-Task: {{TASK}}` through the connection's custom headers
(Open WebUI 0.10.0+):

```json
{"X-OpenWebUI-Task": "{{TASK}}"}
```

An explicitly marked task uses the isolated path even with `stream=true`, and
then streams its output as SSE. Recognized task names are logged at INFO level;
arbitrary header values appear as `other`. Empty task headers allow ordinary
streaming chat to resume. Without the task header, a streaming background request
cannot reliably be distinguished from a normal chat turn.

## Profiles

Claude profiles expose `Read`, `Glob`, and `Grep` plus explicitly configured
Open WebUI Knowledge/Memory tools. Both the available tools
and auto-approved tools are restricted, and a pre-tool hook denies tools outside
the list. Approval requests are denied because there is no approval UI. Host and
project settings/MCP configuration are disabled; the bridge injects its own
Open WebUI MCP server when enabled. `permission_mode` supports
`default` and `plan`; `model`, `system_prompt`, and `max_turns` are configurable.
This is a read-only tool policy, not a filesystem jail: read tools may access
files outside `cwd` that the host account can read.

To enable Codex, install/authenticate its CLI and enable its example profile, then
restart. Its model and appended developer instructions are configurable. It uses
`read-only` sandboxing and `approval_policy="never"` for new and resumed turns,
ignores user config and exec rules, and runs with the configured cwd. Claude tool
names, plan mode, and max_turns do not apply to Codex. It requires
`allowed_tools=[]` and `permission_mode="default"`. On disconnect the proxy
terminates its process group, then escalates to SIGKILL if needed.

## Agent-driven Open WebUI Knowledge and Memory

Both Claude and Codex can search Open WebUI directly during their turns using
the bridge's built-in stdio MCP adapter. No separate MCP server installation or
Open WebUI tool-server connection is needed. Only Knowledge and Memory are
exposed; Open WebUI's other built-in tools are not forwarded.

Add shared defaults in `profiles.toml` to enable every Claude and Codex profile:

```toml
[openwebui]
url = "http://127.0.0.1:3000" # Host-reachable Open WebUI URL, without /api or /v1.
api_key_env = "OPENWEBUI_API_KEY"
knowledge = true
memory = true
memory_write = false
knowledge_ids = [] # All accessible bases; set IDs to restrict access.
timeout_seconds = 30

# Optional: override just these settings for one profile.
[profiles."codex:myrepo".openwebui]
api_key_env = "CODEX_OPENWEBUI_API_KEY"
knowledge_ids = ["collection-id"]
```

Profile settings override shared settings; lists replace the inherited list.
To disable inherited access for one profile, put `openwebui = false` inside its
`[profiles."model-name"]` table, before any nested tables. Without `[openwebui]`,
profiles can still configure their own complete `.openwebui` table as before.
These defaults share one configured account's access across profiles and users;
they do not select a different API key for each Open WebUI user. Background task
requests still run without Knowledge or Memory tools.

Set your Open WebUI **user API key** as `OPENWEBUI_API_KEY` in `.env` or export it
before starting the bridge, then restart it. This key is separate from `PROXY_API_KEY`. Create it
under **Settings → Account → API Keys** after enabling API keys for that account.
See [Open WebUI API keys](https://docs.openwebui.com/features/authentication-access/api-keys/).
Keep the secret out of `profiles.toml`. A missing key returns HTTP 503 before the
agent starts. Different profiles can name different key environment variables.

| Setting | Effect |
| --- | --- |
| `knowledge = true` | List bases, search relevant passages, list source files, read extracted source text |
| `memory = true` | List and semantically search the configured account's memories |
| `memory_write = true` | Also allow adding, updating, and deleting individual memories |
| `knowledge_ids` | Optional allowlist applied to every Knowledge operation |

`add_memory` accepts `content`, optional `path`, and optional `type` (`user` or
`context`). For example, ask: “Remember that this project uses uv, under
`projects/openwebui-bridge`, as a context memory.” Paths organize memories in
Open WebUI; they do not access the filesystem or automatically scope access to
the profile's working directory. The agent can use your specified path or choose
a relevant one. Omitting type uses Open WebUI's default.

`update_memory` accepts the memory ID and any combination of `content`, `path`,
and `type`. Omitted or null fields remain unchanged; an empty path (`""`) clears
the path. Listing memories returns any path/type metadata supplied by Open WebUI.
These fields are available to both Claude and Codex through the same adapter.
Older Open WebUI versions may ignore path/type fields.

Memory writes are disabled by default. When enabled, the bridge auto-approves
only those configured MCP tools on both backends, including Codex's explicit
per-tool approval rules. Its read-only filesystem sandbox stays in effect;
Memory writes happen through Open WebUI's API. Disabling a feature removes its
tools entirely. The agent is instructed to write memories only when requested.
Background title/tag tasks receive no MCP tools, even on an enabled profile.

All calls use the profile's configured API-key owner and Open WebUI's access
checks. Forwarded user/chat IDs do not impersonate that owner. Everyone who can
select that bridge profile can use its configured account's Knowledge/Memory;
for multiple users, assign separate profiles and keys with model access controls.
Changing the key or integration configuration starts a separate native session.

These tools use Open WebUI's Knowledge, file-text, retrieval, and Memory APIs;
they do not call its chat-completions endpoint, so they cannot recurse into the
bridge. Embedding-backed searches require working Open WebUI embeddings. Memory
must be enabled and permitted for the API-key owner. If API-key endpoint
restrictions are enabled, allow the relevant `/api/v1/knowledge/`,
`/api/v1/retrieval/query/collection`, `/api/v1/files/`, and `/api/v1/memories/`
routes according to your version's restriction rules. HTTP failures return a
sanitized tool error without upstream bodies or credentials. Responses above
4 MiB are rejected. Native Open WebUI Knowledge bases are supported; external
Knowledge connectors using separate retrieval APIs are not yet supported.

Check your configured connection without invoking a model or changing memories:

```sh
uv run python -m openwebui_bridge.openwebui_mcp \
  --config profiles.toml --profile 'claude-code:myrepo'
```

Then ask the agent to search a Knowledge base and report a source file. With
`memory_write = true`, ask it to remember a preference, then verify it in Open
WebUI's Memory settings. MCP tools appear as inline progress during streaming.
Open WebUI's native tool toggles do not supply these tools; the profile does.

## Verify

```sh
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
```

Tests exercise SSE output, native resume, prefix changes, tool-line hashing,
restart persistence, user/profile isolation, serialization, errors, timeout, and
HTTP disconnect. Backend tests use actual SDK message types and local subprocess
fixtures. They do not call paid models.

The MCP tests cover real stdio discovery, authenticated API requests, feature
toggles, Knowledge membership checks, memory operations, credential isolation,
and tool permissions on both backends. For an authenticated agent integration
test against a disposable local API fixture (no real Open WebUI data is changed):

```sh
uv run python tests/live_openwebui_smoke.py # Both Claude and Codex.
```

This makes two turns per backend and verifies actual tool execution, Knowledge
search/read, Memory search/write/delete, and native resume. It does not replace
the connection check against your installed Open WebUI version.

For an opt-in live SDK test with two small chat turns and an isolated Haiku task
(uses host authentication and writes native session files under the host account):

```sh
uv run python tests/live_smoke.py
```

It confirms the task returns a JSON completion without changing the chat mapping,
then the second chat turn resumes and remembers context while the backend receives
only the new prompt. The test database and cwd are temporary.

To verify image recognition and image context across native session resume:

```sh
uv run python tests/live_image_smoke.py
uv run python tests/live_image_smoke.py --backend codex
```

This authenticated test generates PNGs in memory and makes three small agent
turns. It checks image recognition, remembered context after the original
temporary files are removed, and a new image attached to a resumed session.

Manual host smoke test (calls Claude):

```sh
curl -N http://127.0.0.1:8081/v1/chat/completions \
  -H "Authorization: Bearer $PROXY_API_KEY" \
  -H 'Content-Type: application/json' \
  -H 'X-OpenWebUI-User-Id: local-user' \
  -H 'X-OpenWebUI-Chat-Id: smoke-test' \
  -d '{"model":"claude-code:myrepo","stream":true,"messages":[{"role":"user","content":"What files are in this repository?"}]}'
```

In Open WebUI, check that text/tool lines arrive before completion, then ask a
follow-up, regenerate it, edit an earlier prompt, and stop a running turn. The
automated tests establish the HTTP/session behavior; a live authenticated agent
run and verification in an actual Open WebUI instance are separate integration
checks.

Local validation on 2026-10-08: 57 automated tests passed, lint and formatting
checks passed, and the authenticated Claude SDK smoke test passed with streaming,
an isolated Haiku JSON task, and native session resume after the task. The
authenticated image smoke test also passed image recognition and native resume.
Local validation on 2026-10-09: 63 automated tests passed, lint and formatting
checks passed, and the authenticated Codex image test passed all three turns:
image recognition, native resume after attachment cleanup, and a new image on a
resumed turn. An actual Open WebUI connection has not yet been verified.

Knowledge/Memory validation on 2026-10-09: 87 automated tests passed. Live
authenticated Claude and Codex turns exercised all six read/search tools and
all three opt-in Memory write tools against the API fixture, including resume.
The user's actual Open WebUI URL/API-key connection has not yet been verified.

