# Generated-image delivery investigation

## Observed transport

On 2026-11-05, a real dinosaur-emoji request ran through the installed
`codex-cli 0.166.0`, with the bridge's `codex exec --json`, read-only sandbox,
`approval_policy="never"`, `--ignore-user-config`, and `--ignore-rules` flags.
The request completed and generated a PNG. This was actual generation, not just
reading the imagegen skill or emitting an acknowledgment.

The captured stdout contained `thread.started`, `turn.started`, agent messages,
skill-read command activity, and `turn.completed`. **It contained no image item,
base64 data, file path, or artifact reference.** The final agent text claimed
generation had completed. Stdout alone could not distinguish missing tool
invocation from dropped image output.

The corresponding local session log confirmed an image tool invocation and
contained this completed native item (image bytes omitted here):

```json
{
  "type": "event_msg",
  "payload": {
    "type": "item_completed",
    "thread_id": "<active native session UUID>",
    "turn_id": "<current turn UUID>",
    "item": {
      "type": "Extension",
      "kind": "image_gen.generation",
      "id": "exec-<UUID>",
      "status": "completed",
      "revisedPrompt": "Create a single adorable dinosaur emoji...",
      "result": "<base64 PNG bytes>",
      "transparentBackground": true,
      "failure": null,
      "savedPath": "<CODEX_HOME>/generated_images/<session>/<item>.png",
      "generationId": "<UUID>"
    }
  }
}
```

The tool's native response also contained an `input_image` data URI. The
generated PNG existed on disk. The bridge uses the completed item's inline
`result` bytes; it never opens `savedPath` or a model-supplied file/URL.

The private raw capture was written to `/private/tmp/bridge-image-raw.jsonl`;
the matching native session was `01a1adb2-32b7-7182-a69e-f0af38e82f80`. Raw logs
contain local instructions and image bytes and are deliberately not committed.
The test fixture preserves the observed native shape with a small generated PNG.

## Compatibility reader and limits

The bridge supplements exec JSONL using the active session's native log under
`$CODEX_HOME/sessions`. Resumed sessions start reading at their pre-invocation
file offset. Only completed native items matching the current thread/turn are
processed. Agent-message boundaries keep image results between the corresponding
text messages even when the log has already been fully written. Image hashes
deduplicate native and direct results independently of tool activity labels.

This is a compatibility workaround for a transport omission, **not a stable
Codex API contract**. It depends on locally readable, persisted session logs
and the observed private event format. Ephemeral/remote transports and future
log-format changes can prevent recovery; stdout has no reliable marker proving
that a missing image was generated. `codex_image_log = false` disables the reader.
Prefer a future transport that directly exposes completed image results when
the installed integration supports it.

Completed MCP `result.content` image blocks (`type`, `data`, `mimeType`) and
embedded image resource blobs are supported. Bare resource links are skipped
without a delivery error; no arbitrary file access, URL fetch, or internal-only
artifact reference is exposed. MCP tests exercise the adapter with real Python
subprocesses emitting protocol fixtures; no live image-generating MCP service
was used.

Limits: 16 MiB per event, 64 MiB per native-log scan, 10 MiB and 32 million
pixels per image, 20 unique images and 40 MiB per turn. Pillow verifies each
accepted PNG/JPEG/GIF/WebP and its MIME type. Invalid, unavailable, oversized,
or unstorable images fail explicitly instead of emitting a delivery claim.

## Browser delivery and retention

**Images are uploaded to Open WebUI when a profile has an `[openwebui]` connection
and key (`/api/v1/files/` then `/api/v1/files/<id>/content`); otherwise, or on any
upload failure, they are delivered as inline base64 Markdown.** The remainder
describes inline delivery. Image bytes are in the assistant
message displayed and saved by Open WebUI. There is no hostname, bridge API
header, or bridge expiration dependency. Access and retention
follow the saved conversation, and export size grows by roughly one-third over
the image bytes. Fresh-session transcript replay forwards these images as vision
inputs instead of sending the base64 as text tokens.

Open WebUI 0.12.4's installed `MarkdownInlineTokens.svelte`, `Image.svelte`, and
`safeImageUrl.ts` were inspected. Markdown images permit data URIs and external
HTTP(S) URLs. The ordinary image component uses a plain `<img>` with no bridge
Authorization header. This is why an authenticated bridge endpoint alone does
not make an image browser-accessible.

Both streaming and non-streaming chat responses render each Image at its event
position and include exactly the same Markdown in the response and persisted
session hash. Non-streaming chat clients must send an explicitly empty
`X-OpenWebUI-Task` header to distinguish foreground chat from legacy isolated
task requests. Nonempty task headers remain tool-free Claude tasks.

## Validation

Automated tests cover the observed native shape, MCP completions after activity
labels, duplicates, invalid MIME/data, truncated images, payload limits, native
log ordering/partial records/resume, streaming/non-streaming rendering, inline vision replay,
displayed/persisted hash equality, native continuation, and delivery errors.

The initial real Open WebUI browser test generated a 1254×1254 dinosaur through
a temporary bridge connection. That test used a browser-facing artifact URL (since removed) and **did not
establish remote reachability**; inline delivery removed that dependency.

The final inline test generated a new 1254×1254 dinosaur through the real
Codex integration in Open WebUI. The browser rendered its `data:image/png;base64`
source, and the saved conversation contained the same complete data URI. Reload
rendered it again. The temporary bridge was then
stopped, and its origin was blocked in the browser: opening the saved
conversation in a fresh browser context still rendered the image. The test
conversation ID is `3c5c7f4c-0782-43a8-a8e1-0d7041eb1f60` (open `/c/<id>` on your
own Open WebUI origin).

The temporary Open WebUI connection was removed and its original connection
configuration restored. The temporary test bridge is stopped. The normal bridge is user-managed; the
user handles loading the updated code.

Final checks: `uv run pytest -q` passed **185 tests**; `uv run ruff check src tests`,
`uv run ruff format --check src tests`, and `git diff --check` passed. No live
image-generating MCP service or remote-browser network was tested. The inline
result has no network URL to resolve; the blocked-origin reload verifies that
its display is independent of bridge network reachability.
