# Agent integration

VocoDeck exposes the same review loop through MCP and the `voco` CLI. The
daemon stays on loopback; remote agents reach it through an SSH
`RemoteForward`. Set `VOCO_URL` for a non-default endpoint and `VOCO_TOKEN`
when `[bridge].token` is configured.

## MCP lifecycle

1. Launch `voco-mcp` as a stdio MCP server from the agent's workspace.
2. Call `voice_init` once. It registers the agent, returns the listening
   instructions, and is safe to repeat after reconnects.
3. Use `voice_screen` for substantial progress and `voice_say` only for a
   short spoken summary.
4. Publish reviewable material with `page_push`. A path must stay inside the
   workspace; inline Markdown needs a name. Diff sources support PR, branch,
   staged changes, and a confined patch file.
5. At the end of a turn, keep the listener returned by `voice_init` running,
   or call `voice_listen`. Spoken commands and workbench review items arrive
   through the same loop.
6. Call `review_findings` before declaring review complete. Close every item
   with `review_reply`: findings take `addressed`, `disputed`, or `wont-fix`;
   questions and asks take a Markdown answer. Replies are idempotent.

Re-pushing the same page updates it in place. Diff revisions preserve stale
finding markers so the agent can distinguish old feedback from feedback on
the current content.

## Failure and restart contract

- A daemon restart preserves queued commands, pages, findings, asks, and
  session credentials, but restored agents remain disconnected until they
  actually call back. Speech is never routed to a ghost session.
- MCP and CLI calls may be retried. Exact duplicate review replies and page
  updates converge without creating duplicate work.
- A WebSocket client that falls behind is closed with a retryable resync code;
  reconnecting starts with an authoritative snapshot.
- HTTP 4xx responses describe caller mistakes. Unexpected 5xx responses are
  sanitized for clients and recorded with traceback details in daemon logs.

## Data retention

Input and speech history are capped at 50 entries per session, and each agent
screen is updated in place. Workspace pages, findings, and asks are durable
and are not silently deleted. Export a review with `voco review export`
before manually archiving a workspace manifest under
`~/.local/share/voco/workspaces/` (or `[workbench].data_dir`). Automatic
destructive pruning is intentionally disabled until an operator chooses a
retention policy.
