# Open WebUI Bridge

Open WebUI Bridge lets you use Claude Code and Codex through the same Open WebUI
interface. It also gives these agents access to Open WebUI Knowledge, Memory,
Notes, chat history, and remote MCP tools.

## Start the bridge

You need macOS or Linux, Python 3.11+, and `uv`.

1. Authenticate Claude with `claude auth login` using an installed Claude CLI,
   or set `ANTHROPIC_API_KEY` in `.env` below.
2. Install and copy the example configuration (skip the copies if you already
   have your own files):

   ```sh
   uv sync
   cp profiles.example.toml profiles.toml
   cp .env.example .env
   ```

3. In `profiles.toml`, set each profile's `cwd` to your repository directory.
   Relative paths start from the directory containing `profiles.toml`.
4. In `.env`, replace `PROXY_API_KEY` with a private key of your choice.
5. Start the server:

   ```sh
   uv run openwebui-bridge --config profiles.toml
   ```

Restart the bridge after changing `profiles.toml` or `.env`. Existing environment
variables take precedence over `.env`. Run one bridge process per database.

## Connect Open WebUI

1. Set `ENABLE_FORWARD_USER_INFO_HEADERS=true` in Open WebUI's environment and
   restart Open WebUI. Use plain user-info forwarding; keep the default header names.
2. Add an **OpenAI connection** with:

   | Field | Value |
   | --- | --- |
   | URL | `http://127.0.0.1:8081/v1` |
   | API key | Your `PROXY_API_KEY` value from `.env` |

3. Add these **Custom Headers** to the connection:

   ```json
   {
     "X-OpenWebUI-Chat-Id": "{{CHAT_ID}}",
     "X-OpenWebUI-Message-Id": "{{MESSAGE_ID}}",
     "X-OpenWebUI-User-Message-Id": "{{USER_MESSAGE_ID}}",
     "X-OpenWebUI-User-Message-Parent-Id": "{{USER_MESSAGE_PARENT_ID}}",
     "X-OpenWebUI-Task": "{{TASK}}"
   }
   ```

   The task placeholder requires Open WebUI 0.10.0+.
4. Select `claude-code:myrepo` (from the example config), keep streaming enabled,
   and ask: “What files are in this repository?”

If Open WebUI runs in Docker on macOS, use
`http://host.docker.internal:8081/v1` and change `host` under `[server]` to
`"0.0.0.0"`. On Linux, configure host gateway networking or use host networking.
Set `PROXY_API_KEY` before making the bridge reachable outside localhost.

## Add or change a profile

Add a table to `profiles.toml`. Its name becomes the model name in Open WebUI:

```toml
[profiles."claude-code:myrepo"]
backend = "claude"
cwd = "/absolute/path/to/myrepo"
model = "sonnet"
allowed_tools = ["Read", "Glob", "Grep"]
system_prompt = "Help the user understand this repository."
```

To let Claude edit files and run commands, change that profile's `allowed_tools`:

```toml
allowed_tools = ["Read", "Glob", "Grep", "Edit", "MultiEdit", "Write", "Bash"]
```

Tools run unattended as your host account. Scope access when needed, for example
`"Bash(git diff:*)"` or `"Edit(./src/**)"`.

For Codex, install its CLI, run `codex login`, and use a profile like this
(the example config already includes one):

```toml
[profiles."codex:myrepo"]
backend = "codex"
cwd = "/absolute/path/to/myrepo"
permission_mode = "default"
allowed_tools = []
```

Codex uses a read-only filesystem sandbox. Title, tag, and follow-up requests
still require Claude authentication; choose a separate task model in Open WebUI
if you only want to authenticate Codex.

Profiles load `CLAUDE.md` or `AGENTS.md` from their project. Add
`project_instructions = false` to a profile to disable this.

You can switch an existing chat to a bridge model even if its history contains
tool calls from another model. The bridge keeps assistant text, discards client
tool calls and tool replies, and logs a warning. The new agent will not see those
tool results; ask it to fetch any needed information again.

## Attach an image

In Open WebUI, open **Workspace → Models**, edit your profile's model, and enable
**Vision** and **File Upload**. Attach a screenshot and send your question.

Use PNG, JPEG, GIF, or WebP: up to 5 MiB per image, 20 images and 20 MiB total
per request, including history. For API clients, send inline base64 `image_url`
parts; external image URLs are unsupported.

## Display generated images

Ask your Codex profile to generate a dinosaur emoji. When the profile has an
`[openwebui]` connection and its API key is set, the bridge uploads the image to
Open WebUI's file store and emits a same-origin link
(`/api/v1/files/<id>/content`). Messages stay small, the browser loads the image
from Open WebUI with its normal session, and Open WebUI owns retention and
access. The file belongs to the API key's account, so multi-user deployments may
not let other users view it. Set `upload_images = false` under `[openwebui]` to
opt out.

Without an `[openwebui]` connection or key, or if an upload fails, the bridge
falls back to inline base64 Markdown, which works for remote browsers and needs
no extra access. Base64 adds about one-third to message and export size.

Keep streaming enabled. For a non-streaming chat API request, explicitly send an
empty `X-OpenWebUI-Task` header; without that header, non-streaming requests keep
using the isolated, tool-free Claude task profile. Nonempty task headers always
select the task profile. Image Markdown is identical in the displayed response
and the session hash. On a fresh session or branch, earlier inline images are
forwarded as vision inputs rather than base64 text tokens; images stored in
Open WebUI are fetched back with the profile's API key. If a fetch fails, the
model sees only the link.

Payload limits are 10 MiB per image, 20 images / 40 MiB per turn, and 32 million
pixels per image; only validated PNG, JPEG, GIF and WebP are accepted. Base64
adds about one-third to message and export size. Conversation access and
retention are controlled by Open WebUI; images are not separately uploaded to
its file store. Deployments must allow `data:` images in their content security
policy. Text-only clients may show raw Markdown.

Codex CLI 0.166.0 generates images but omits `image_gen.generation` completions
from `exec --json`. The bridge supplements that stream with completed native
image events from the active session's log under `$CODEX_HOME/sessions`
(default `~/.codex/sessions`). This requires persisted, locally readable session
logs and depends on Codex's private log format. It reads inline base64 results,
never `savedPath` or arbitrary local files. Set `codex_image_log = false` in a
profile to disable this compatibility reader. Completed MCP image blocks are
also supported directly. Resource links without inline image bytes are
skipped. A tool acknowledgment alone is not an image.

See [image-delivery investigation](docs/generated-images.md) for the observed
transport format and validation evidence.

## Enable Open WebUI tools

1. Create an Open WebUI user API key under **Settings → Account → API Keys**.
   API keys must be enabled for that account.
2. Put it in `.env` as `OPENWEBUI_API_KEY`. This is separate from `PROXY_API_KEY`.
3. Add or edit this top-level table in `profiles.toml`, outside any profile:

   ```toml
   [openwebui]
   url = "http://127.0.0.1:3000" # Your host-reachable WebUI URL, without /api or /v1.
   api_key_env = "OPENWEBUI_API_KEY"
   knowledge = true
   memory = false
   memory_write = false
   notes = false
   notes_write = false
   chat_history = false
   ```

4. Enable the features you need, restart the bridge, and check the connection:

   ```sh
   uv run python -m openwebui_bridge.openwebui_mcp \
     --config profiles.toml --profile 'claude-code:myrepo'
   ```

| To… | Set… | Then ask… |
| --- | --- | --- |
| Search Knowledge | `knowledge = true` | “Find the deployment guide and cite its source.” |
| Search memories | `memory = true` | “What preferences have I saved?” |
| Save or change memories | Also `memory_write = true` | “Remember that this project uses uv.” |
| Read Notes | `notes = true` | “Find my deployment checklist.” |
| Write Notes | Also `notes_write = true` | “Save this summary as a note.” |
| Search old chats | `chat_history = true` | “Find our earlier deployment discussion.” |

To organize memories by project, add `memory_path = "projects/myrepo"` to
`[openwebui]`, or override it for one profile:

```toml
[profiles."claude-code:myrepo".openwebui]
memory_path = "projects/myrepo"
```

Use the same path for Claude and Codex to share project memories. To disable all
Open WebUI tools for a profile, put `openwebui = false` inside its main table,
before any nested tables.

Everyone using a profile gets the configured API-key account's access. Use
separate profiles and keys for separate accounts. Enabled writes run without
confirmation; memory paths organize retrieval and do not restrict access.

## Add MCP tools

For a remote Streamable HTTP server, add a top-level table in `profiles.toml`:

```toml
[mcp_servers.litellm]
url = "http://127.0.0.1:4000/mcp"
api_key_env = "LITELLM_API_KEY" # Omit for an unauthenticated server.
prefix = "tools_" # Optional; defaults to litellm_. Use "" for no prefix.
```

Put the token in `.env`, restart, and run the connection check above. This works
without an `[openwebui]` connection. All tools available to the upstream key are
auto-approved, so limit access using that key's permissions.

For project-local MCP servers, add these settings inside the profile's main table:

```toml
project_mcp = true
allowed_tools = ["mcp__myserver"]
```

Keep any other needed Claude tools in `allowed_tools`. Configure the server in
`cwd/.mcp.json` for Claude or `cwd/.codex/config.toml` for Codex. To allow just one
tool, use `mcp__myserver__toolname` instead.

## Troubleshooting

| Problem | Try |
| --- | --- |
| Open WebUI cannot connect | Run `curl http://127.0.0.1:8081/health`; check the URL, Docker networking, and proxy API key. |
| Model missing from the list | Check the profile name and `cwd`, restart the bridge, and refresh Open WebUI's model list. |
| Agent cannot edit files | Add Claude editing tools to the profile. Codex profiles use a read-only sandbox. |
| Knowledge or Memory fails | Run the connection check; verify `OPENWEBUI_API_KEY`, account permissions, API endpoint restrictions, and embeddings. |
| Follow-ups lose context | Keep user-info forwarding, custom headers, and streaming enabled. Filters that rewrite history can force a fresh session. |
| Extra title or tag requests | Choose a separate task model in Open WebUI, or set `[server]`'s `task_model` (default `"haiku"`). |

For development commands, see [AGENTS.md](AGENTS.md).
