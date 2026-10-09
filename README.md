# Open WebUI CLI proxy

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
export PROXY_API_KEY='choose-a-private-key'
uv run openwebui-cli-proxy --config profiles.toml
```

The example listens on `127.0.0.1:8081`. Profiles and paths are loaded once at
startup. Relative paths resolve against the configuration file. The model ID
selects a profile; clients cannot override its directory, tools, or agent model.
Restart after changing configuration. Use one process/worker per SQLite database;
an exclusive file lock rejects a second process using the same database.

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

Text strings and lists of text parts are supported. Images/audio, conversation
history containing client tool calls/results, assistant prefill, multiple choices,
and structured response formats return HTTP 400.

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
  new user text.
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

Claude profiles expose only `Read`, `Glob`, and `Grep`. Both the available tools
and auto-approved tools are restricted, and a pre-tool hook denies tools outside
the list. Approval requests are denied because there is no approval UI. Host and
project settings/MCP configuration are disabled. `permission_mode` supports
`default` and `plan`; `model`, `system_prompt`, and `max_turns` are configurable.
This is a read-only tool policy, not a filesystem jail: read tools may access
files outside `cwd` that the host account can read.

To enable Codex, uncomment the example profile, install/authenticate its CLI, and
restart. Its model and appended developer instructions are configurable. It uses
`read-only` sandboxing and `approval_policy="never"` for new and resumed turns,
ignores user config and exec rules, and runs with the configured cwd. Claude tool
names, plan mode, and max_turns do not apply to Codex. It requires
`allowed_tools=[]` and `permission_mode="default"`. On disconnect the proxy
terminates its process group, then escalates to SIGKILL if needed.

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

For an opt-in live SDK test with two small chat turns and an isolated Haiku task
(uses host authentication and writes native session files under the host account):

```sh
uv run python tests/live_smoke.py
```

It confirms the task returns a JSON completion without changing the chat mapping,
then the second chat turn resumes and remembers context while the backend receives
only the new prompt. The test database and cwd are temporary.

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

Local validation on 2026-10-08: 43 automated tests passed, lint and formatting
checks passed, and the authenticated Claude SDK smoke test passed with streaming,
an isolated Haiku JSON task, and native session resume after the task. An actual
Open WebUI connection and a live Codex turn have not yet been verified.

