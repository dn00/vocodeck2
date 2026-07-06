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

## W0 build order — **COMPLETE 2026-07-06**

1. [x] `core/workspace.py` — realpath(root) key (+ host), sessionspace
       fallback, repo-group via common_dir, in-memory pages (screen
       upsert, doc push, rev bump), snapshot = metadata-only. Pure,
       injected clock/emit. 10 tests.
2. [x] `protocol/messages.py` — workspace/page/finding/ask/term events
       + workspace/page/finding/ask/review commands; PROTOCOL.md
       regenerated (30 events, 20 commands).
3. [x] §8.5 auth in `server/http.py` — loopback-any-port Origin
       discipline (WS upgrade + all mutating routes), per-run wb token
       minted + injected into served pages, `allowed_origins` config,
       bearer gating widened. WS commands gated on wb for browser
       origins. Foundation gap (no Origin check) closed here. 10 tests.
4. [x] `server/workbench.py` — `/` shell with CSP + nonce, `/static/*`,
       `/v1/page/{id}` (read fresh, realpath-confined per read, size/
       binary caps), `POST /v1/bridge/page` (doc; local_fs cell for
       remote), `ui.html` → `/debug`. workspace.list/page.close/reopen
       control commands.
5. [x] Client (`static/`): store (+subscribe seam), bus (WS,
       self-healing), markdown (vendored marked+DOMPurify, pinned, ESM
       wrappers, plaintext fallback), app (rail repos→workspaces→agents
       + roster, tabstrip, dock/status placeholders). tsc --checkJs
       clean.
6. [x] `agent_state.py` — total-precedence display state; dots in rail.
7. [x] tsc CI step (Linux, `npx -p typescript@5.6.3 tsc`); verified
       green locally.
8. [x] Debug UI relocated to `/debug` (+ `/ui` alias kept); wb token
       plumbed into its WS. README pointer update pending W-final.
9. [x] E2E smoke: daemon boots headless, register→workspace, screen +
       path-doc pages present, `/` serves shell + static 200, page
       content resolves (doc read-fresh), foreign origin → 403.
       **Pending user AM: live browser click-through** (no headless
       browser in this env; all pieces verified at HTTP + module level).

Gates at W0 close: ruff clean, ruff format clean, mypy clean (39 files),
188 pytest passing, tsc --checkJs clean, PROTOCOL.md in sync.

## Milestones (W1–W5 tracked as reached; definitions in SPEC §11)

- [x] **W0 — pages + shell + auth** (complete 2026-07-06; see build order above)
- [x] **W1 — diff review** (complete 2026-07-06): diff page (pr/branch/
      staged/file + raw content, git in workspace root, local_fs cell) +
      unified-diff parser ported from the oracle + click/shift-click
      annotation + findings ledger (add/update/status/withdraw, stale on
      rev bump) + findings dock + `voco review export` (legacy JSON +
      anchors sidecar, byte-compatible) + durable per-workspace manifests
      + daemon-level single-writer lock. Verified e2e incl. restart
      persistence. Tests: diff/findings/manifest/http (205 total green).
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

## RESUME HERE (checkpoint 2026-07-06, mid-session model switch)

State of the uncommitted checkpoint that this commit captures:

**DONE + VERIFIED (green: 207 tests, mypy, ruff, tsc all clean):**
- W0 + W1 shipped in prior commits (see journal).
- **Codex adversarial review of W0+W1 applied** (review-out.txt had 4
  BLOCKER + 7 WARNING/NOTE). Fixes landed in THIS checkpoint:
  1. BLOCKER — CSWSH read leak: browser-origin WS now needs the wb token
     to even READ `/v1/events` (`server/http.py:_events_ws`). Tested.
  2. BLOCKER — path confinement TOCTOU: `confined_read` is now fd-based
     (open→fstat→read from fd), relative paths resolve against root.
  3. BLOCKER — `diff_file` confinement was discarded; now the confined
     content IS the diff (never re-opened unconfined).
  4. BLOCKER — manifest lock now atomic (`O_CREAT|O_EXCL`) + safe takeover.
  5. WARNING — agents can't resurrect withdrawn findings.
  6. WARNING — `default_branch` keeps `origin/main` (right merge-base).
  7. WARNING — git/gh option-injection guards (`pr` digits, `_valid_ref`, `--`).
  8. WARNING — pasted-diff byte cap (MAX_DIFF_BYTES).
  9. WARNING — quoted git paths: DEFERRED, noted in `core/diff.py` (oracle
     shares the limitation).
  10. WARNING — relative-path cwd bug folded into fix 2.
  11. NOTE — CSP `connect-src` tightened to `'self'`.

**IN PROGRESS — W2 (the wake), scaffolded but NOT verified, NO tests yet:**
- `core/registry.py`: `review_items` hook + `on_listen_start` returns
  `{status:"review", items}` / rides `queued`; `wake_review()`.
- `core/workspace.py`: `Ask` model, `add_ask`/`answer_ask`/`answer_finding`,
  `pending_review()`; asks in dump/restore.
- `daemon.py`: `_primary_session` election, `_review_items_for`,
  `_wire_review_wake` (wakes primary on finding.added/ask.created).
- `server/workbench.py`: `ask.create`/`ask.list` commands, `ask_reply`
  bridge verb (answers ask or question-finding).
- `protocol/messages.py`: ask.create/ask.list commands added.

**W2 REMAINING before it can be called done:**
- [ ] MCP tools: `page_push`, `review_findings`, `review_reply` in
      clients/voco_mcp; voco-mcp registers `review` capability.
- [ ] voco-cli/voco-mcp register with `review` capability by default.
- [ ] Agent discipline text += review line (SPEC §8.4 block).
- [ ] Client: chat/ask dock panel; render finding.answer on the card;
      store handling for ask.created/ask.answered events.
- [ ] TESTS: wake-on-finding delivers to parked listen; at-least-once
      redelivery after a missed wake; primary election with 2 agents;
      ask round-trip; asks survive restart. (None written yet.)
- [ ] E2E: annotate → parked agent wakes via listen → marks addressed.
- [ ] fab-router discipline owed: run fab-trust-boundaries sink-table
      pass + fab-adversarial-self-review charges on the W2 diff before
      the W2 "done" commit (skipped on W0/W1 — Codex caught what the
      skill would have; do not repeat).

## W2 build order (original)

1. [ ] `review` capability: registered like say/listen; gates the wake.
2. [ ] `listen` gains `{status: "review", items}` + review items ride
       `queued`; at-least-once redelivery until status leaves open /
       ask answered; idempotent by item id; no turn_id minted.
3. [ ] Ask/chat: `ask.create` (browser) → primary-agent election →
       `ask_reply` bridge verb → renders under the chat/finding card.
4. [ ] MCP tools: `page_push`, `review_findings`, `review_reply`
       (answers ask OR sets finding status); voco-mcp registers
       `review` capability.
5. [ ] Agent discipline text += review line.
6. [ ] Client: chat dock panel; finding.answer renders on the card.
7. [ ] W2 exit: annotate a line → parked agent wakes via listen →
       marks addressed → chip flips; kill mid-wake → redelivers.

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
- **2026-07-06 (W0 shipped)** — Full workbench foundation built native,
  no subagents. New: `core/workspace.py`, `core/agent_state.py`,
  `server/workbench.py`, `static/` client (store/bus/markdown/app +
  vendored marked+DOMPurify), §8.5 browser auth in `server/http.py`
  (closes a real foundation gap: verified no Origin check existed).
  Screen verb now doubles as a pinned page with wire compat intact.
  20 new tests (188 total green); mypy/ruff/tsc all clean. E2E smoke
  passed at HTTP level. Adhoc improvements: mypy grammar bumped to 3.12
  (numpy 2.4 PEP-695 stubs were unparseable — pre-existing latent CI
  risk), CLI now derives `common_dir` for rail grouping. Committing +
  pushing as milestone 1.
- **2026-07-06 (W0 self-review fix)** — Caught + fixed a regression the
  HTTP smoke missed: the §8.5 CSP would block the debug UI's inline
  script. Nonced it; verified nonces match on both `/` and `/debug`.
- **2026-07-06 (W1 shipped)** — Diff review end-to-end. New:
  `core/diff.py` (parser ported from oracle), `adapters/diffsource.py`
  (git/gh in workspace root), findings ledger on `core/workspace.py`,
  `core/review_export.py` (legacy JSON + sidecar), `adapters/manifest.py`
  (durable per-workspace manifests + daemon-level lock w/ start-time
  nonce), client `diff.mjs` + `findings.mjs`. Verified: real branch diff
  resolved, finding round-trips agent↔human, export byte-compatible,
  review survives daemon restart. 205 tests green. Two commits (core +
  persistence). Adhoc: extracted `handle_workbench_command` shared by
  daemon + tests. Next: Codex review of W0+W1, then W2 (the wake).
