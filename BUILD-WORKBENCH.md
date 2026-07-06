# BUILD-WORKBENCH — plan + running journal

Read SPEC-WORKBENCH.md first (and SPEC.md for the foundation); this file
tracks execution state for the workbench effort so any session can
resume. Update the journal at every checkpoint (after each commit).

All work on branch **`workbench`** (user decision, 2026-07-06).

## Working rules (carried from BUILD.md, 2026-07-03)

- No subagents; build in the main session.
- Journal progress here at each checkpoint (session-crash insurance).
- Work continuously until a milestone checkpoint; commit per coherent step.
- It's Claude's project: Claude decides build order and details within
  the spec.
- New for this effort: diff-annotate (`claude-setup/skills/diff-annotate`)
  is the reference oracle — port its tests per slice BEFORE the
  implementation they cover; keep output JSON contracts byte-compatible.

## Phase 0 — spec finalization gate — **CLOSED 2026-07-06**

1. [x] Grill — 7 decisions, SPEC-WORKBENCH.md §13 entries 20–26
       (workspace key root-only; primary election; loopback-any-port
       Origin policy; terminal interactive-on-focus; XDG data dir;
       export cwd-resolved + data-dir default; slice order stands).
2. [x] Adversarial review — /xai (Codex, 40 findings; ~36 verified +
       applied as v0.2, 1 rejected as a misread; §13 entries 13–19).
3. [x] Spec bumped to **v1.0** — ready for build.
4. [ ] Deferred verification (at W4 start, not a gate): pywinpty
       version floor + ConPTY minimum Windows build.

## W0 build order (current)

1. [ ] `core/workspace.py` — workspace identity (realpath(root) key,
       sessionspace fallback, repo-group via common_dir) + in-memory
       pages model (screen upsert, doc push, rev bump); pure, injected
       git-facts. Port diff-annotate workspace/subject tests first.
2. [ ] `protocol/` — page/workspace/finding/ask event + command
       vocabulary, snapshot extension, `review` capability flag;
       regenerate PROTOCOL.md (CI drift check keeps honesty).
3. [ ] §8.5 auth in `server/http.py` — loopback-any-port Origin
       discipline (WS upgrade + mutating routes), per-run workbench
       token minted + injected into served page, bearer gating
       widened to all mutating surfaces. Tests: cross-origin mutation
       rejected; no-Origin curl passes. (Foundation-side fix, lands
       here.)
4. [ ] `server/` routes — static workbench serving at `/` with CSP,
       `ui.html` → `/debug`, page read routes (by id), workspace.list
       + page commands over WS.
5. [ ] Client lift — `static/` bootstrap: panel-api, store,
       editor-tabs, markdown renderer (+ vendored marked/hljs/mermaid/
       DOMPurify + vendor/MANIFEST), bus-client rewritten for the WS
       envelope (snapshot-first); rail (workspaces→agents dots +
       roster), dock, status bar; screen + doc pages rendering live.
6. [ ] `agent_state.py` — total-precedence display state +
       session.state payload extension; dots wired into rail.
7. [ ] JSDoc + pinned `tsc --noEmit --checkJs` CI job (blocking).
8. [ ] README/PROTOCOL pointer updates (`/` = workbench, `/debug` =
       reference client).
9. [ ] W0 exit check: two agents in two worktrees show screen pages
       in one workbench; cross-origin mutation attempt rejected.

## Milestones (W1–W5 tracked as reached; definitions in SPEC §11)

- [ ] **W0 — pages + shell + auth** (build order above)
- [ ] **W1 — diff review**: diff page + annotation + findings ledger +
      manifest persistence + `voco review export`.
- [ ] **W2 — the wake**: `review` capability + listen status + queued
      ride-along + at-least-once redelivery, MCP tools, ask/chat panel,
      discipline text.
- [ ] **W3 — worktrees first-class**: repo grouping, `voco new
      --worktree`, clean-only removal.
- [ ] **W4 — TerminalBackend**: port + pty impl (Unix pty / Windows
      ConPTY), `/v1/term/*` stream, xterm.js page, per-spawn
      `--backend`.
- [ ] **W5 — rev/staleness depth**: inter-diff, since-rev banner, stale
      chips, live-git tracker.

## Journal

- **2026-07-06** — Effort started. Deliberated the diff-annotate merge
  (federate vs port): decided **port into vocodeck2, one package**
  (full decision log: SPEC-WORKBENCH.md §13). Branch `workbench`
  created; SPEC-WORKBENCH.md draft v0.1 written (pages model, unified
  wake, TerminalBackend tmux+pty, worktree workspaces, agent-state
  derivation, W0–W5 slices). Nothing committed yet; spec awaits
  grilling (Phase 0).
- **2026-07-06 (later)** — Adversarial review via /xai (Codex gpt-5.5,
  40 findings). Independently confirmed the headline one against the
  code: no Origin check on WS/control (`server/http.py`) — hostile web
  pages can reach loopback (CSWSH/CSRF); becomes §8.5 mandatory Origin
  + workbench token, plus a foundation-side patch note. Other majors
  applied: adapter-side resolution for remote sessions (`local_fs`
  cell), `review` as a registered capability, at-least-once delivery
  with authoritative ledger, sessionspaces for repo-less sessions,
  snapshot extension, total display-state precedence, DOMPurify+CSP,
  export contract, milestone dependency fixes. Workspace-key fork left
  OPEN for the grill. Spec now **v0.2**. (Env note: fixed a
  self-looping `~/.local/bin/node` symlink blocking `env node` — left
  in place, denied deletion; user should `rm` it.)
- **2026-07-06 (Phase 0 closed)** — Grill run, 7 decisions (§13
  entries 20–26; headline: workspace key = realpath(root) only, after
  checking herdr's actual semantic — user-arranged containers — and
  keeping that as a later UI-arrangement feature, never data
  identity). Spec folded and bumped to **v1.0**; W0 expanded into a
  9-step build order above. Still nothing committed — the two docs +
  branch are the whole diff; commit them as the branch's first commit
  before starting W0 step 1.
