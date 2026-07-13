# VocoDeck 2 — Dogfood Issues (2026-07-13)

## Open

- **DF-7: Diff base defaults to full branch divergence instead of merge-base.**
  `page diff --branch staging` showed 1.7k files (full divergence) instead of just the PR's changes. Default should be merge-base diff. The UI should also let you select a different base.

- **DF-9: Rail nav double-selects across workspaces; page doesn't switch.**
  Clicking "Files" or "Overview" in one workspace's rail also selects the same nav item in the other workspace (firstmate + CRM both highlight). Likely because phantom agents (DF-3/DF-4) share the same selection state or the nav handler matches by item type rather than workspace-scoped ID. The center panel also doesn't switch to the clicked page — required a full page refresh to recover. After refresh the double-select still persists. Would scale to N-select with more workspaces added. Probably related to the phantom agent sessions from DF-3.

## Fixed

- **DF-4: Stale/idle session not selectable and not auto-cleaned.** Existing
  manual detach controls remain available, and idle sessions are now marked
  disconnected after two minutes and automatically removed after fifteen
  minutes without a listener heartbeat.

- **DF-10: "Idle" status misleading for dead sessions; messages silently
  queued.** Idle sessions with no listener heartbeat for two minutes now become
  `disconnected`, render in red, cannot be activated, and reject new input
  without changing queue/history. A returning listener restores `ready` state.

- **DF-6: Separate workspace registration from agent registration.** Added
  `voco workspace add <path>` and a session-free workspace registration path.
  CLI/MCP `page_push` now publishes directly to a workspace and no longer
  creates a voice agent. `voice_init` and listening remain the agent boundary.

- **DF-3: Multiple sessions spawned instead of one; identity keeps changing.**
  `voice_init` now bakes the MCP server's resolved instance, harness, and cwd
  into both generated listener scripts. Monitor/background processes reuse the
  same cached daemon session instead of registering phantom agents.

- **DF-8: No way to close/remove a page.** The existing browser close controls
  now confirm when a page has annotations. Added `voco page close <id>` and the
  MCP `page_close` tool. Closed pages remain durable and reopen when republished.

- **DF-1: Annotation dialog at top of page instead of inline.** File
  annotations now position from the selected range inside the scrolling center
  panel instead of being inserted after the file header. Covered by Chromium
  geometry regression testing.

- **DF-2: Need an annotation mode toggle.** Added a persistent center-panel
  toggle. Normal selection/copy is the default; file, diff, document, screen,
  and overview surfaces only open annotation editors while annotation mode is
  enabled. HTML artifacts remain on their existing element-annotation flow.

- **DF-5: Error toast undismissable.** Fixed by using a semantic dismiss button
  with isolated click handling and keyboard focus styling. Covered by browser
  regression testing.
