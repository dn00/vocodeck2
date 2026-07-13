# VocoDeck 2 — Dogfood Issues (2026-07-13)

## Open

- **DF-1: Annotation dialog at top of page instead of inline.**
  In the file browser, after opening a file and clicking or selecting text, the annotation dialog appears at the top of the page instead of anchored inline next to the selected line.

- **DF-2: Need an annotation mode toggle.**
  Clicking/selecting text always triggers the annotation dialog, which conflicts with normal interactions like copying text. Add a toggle button (top-right of center panel or similar) to switch between annotation mode and normal select/copy mode. Should be available on all page types (files, diffs, docs, overview) except HTML rendered pages.

- **DF-3: Multiple sessions spawned instead of one; identity keeps changing.**
  A single Claude Code session created multiple daemon sessions with different names. `voice_init` registered as "Dana", the streaming listener registered a second session as "Ezra", and then `voice_screen` brought "Dana" back — now both Dana and Ezra appear under the firstmate repo. Expected: one stable session identity throughout. Root cause likely: each MCP tool call and the Monitor script each derive a different session identity (different TMUX_PANE or process context), so the daemon treats them as separate agents. Workspace association was also delayed until a page reload.

- **DF-4: Stale/idle session not selectable and not auto-cleaned.**
  The "Dana" session shows as idle in the workbench and cannot be selected (mic cannot be passed to it). Stale sessions from split identity (DF-3) should either be auto-cleaned after a timeout or have a manual dismiss/remove option in the UI.

- **DF-5: Error toast undismissable.**
  Clicking a stale agent on the deck shows an error toast: `activate failed: no session named 'Dana (ip-172-31-10-253:firstmate)'`. The X button on the toast does nothing — the toast stays stuck on screen with no way to dismiss it.

- **DF-6: Separate workspace registration from agent registration.**
  Currently every interaction (page_push, voice_init, listen) spawns an agent session. This creates phantom agents for repos you're just reviewing. Proposed split: `voco workspace add <path>` registers a repo/worktree as a review surface (diffs, files, pages) with no agent. `voice_init` / `voco listen` registers a live agent inside an already-known workspace. The rail groups by workspace; agents appear as dots inside their workspace. A workspace with no agent is the normal review-only state. This is architectural — it would also resolve DF-3 and DF-4 at the root.

- **DF-7: Diff base defaults to full branch divergence instead of merge-base.**
  `page diff --branch staging` showed 1.7k files (full divergence) instead of just the PR's changes. Default should be merge-base diff. The UI should also let you select a different base.

- **DF-9: Rail nav double-selects across workspaces; page doesn't switch.**
  Clicking "Files" or "Overview" in one workspace's rail also selects the same nav item in the other workspace (firstmate + CRM both highlight). Likely because phantom agents (DF-3/DF-4) share the same selection state or the nav handler matches by item type rather than workspace-scoped ID. The center panel also doesn't switch to the clicked page — required a full page refresh to recover. After refresh the double-select still persists. Would scale to N-select with more workspaces added. Probably related to the phantom agent sessions from DF-3.

- **DF-10: "Idle" status misleading for dead sessions; messages silently queued.**
  Sessions with no active listener (Dana, Silas) show as "idle", implying they're alive and waiting. In reality they're dead processes that will never drain queued messages. Text sent to them queues silently with no feedback. Needs: (a) a "disconnected" status (no listener heartbeat for N seconds) distinct from "idle" (listener parked, waiting for input), (b) auto-reap after a longer timeout, (c) the UI should warn or block sending to a disconnected session. Related to DF-3/DF-4/DF-6.

- **DF-8: No way to close/remove a page.**
  Once a diff or doc is pushed to the workbench, there's no way to close or remove it — not from the UI (no close button on pages in the rail) and not from the CLI or MCP (no `page close` / `page_remove` command). Need both: a close button in the UI (with a confirm dialog for pages with annotations/findings) and a CLI/MCP verb (`voco page close <id>` / `page_close`) so agents can clean up too.

## Fixed
