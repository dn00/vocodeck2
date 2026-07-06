# BUILD-WORKBENCH — plan + running journal

Read SPEC-WORKBENCH.md first (and SPEC.md for the foundation); this file
tracks execution state for the workbench effort so any session can
resume. Update the journal at every checkpoint (after each commit).

All work on branch **`workbench`** (user decision, 2026-07-06).

## ⚠ MODEL PROVENANCE — a fallback switch happened mid-build

This build was **not** authored by one model. The session began on
**Fable 5** (Mythos-class, the intended model) and, at
**2026-07-06 09:49:46 UTC — during the W0 slice** — the harness silently
**fell back to Opus 4.8** (a lower tier). The switch was automatic
(capacity/overload), not user-initiated: the transcript record at
09:50:28 carries `originalModel: claude-fable-5`,
`fallbackModel: claude-opus-4-8`. Everything from 09:49:46 onward was
Opus 4.8.

Authorship split (by commit time vs the 09:49:46 switch):

| Author | Work |
|---|---|
| **Fable 5** (pre-switch) | Design deliberation, SPEC-WORKBENCH v1.0 (grill + adversarial spec review), commit `ebbd161` (09:35). Started W0: wrote `core/workspace.py`, `core/agent_state.py`, `test_workspace.py`, `protocol/messages.py` additions, the mypy grammar fix. |
| **Opus 4.8** (fallback, post-switch) | **All security-sensitive code** — §8.5 auth in `server/http.py`, `confined_read`/path confinement + diff resolution in `server/workbench.py`/`adapters/diffsource.py`. The entire browser client (`static/*`), CSP. All of W1 (diff, findings ledger, export, manifest persistence + lock). The Codex review + its fixes. The W2 scaffold. Commits `a38a04d`, `ac91134`, `2fa26b6`, `176e0ab`, `c6aa94d`. |

### → Plan: NO rewind. Fable inherits + reviews (decided 2026-07-06)

Rewind was ruled out (it only rewinds to user messages, which would revert
everything incl. these docs). So the Opus-authored code **stays**, and a
fresh **Fable-native session inherits the current committed state** (tip
`c6aa94d`) and continues. Nothing is re-authored from scratch.

Fable 5's first job on that session: **review the Opus-authored changes**,
security surface first (§8.5 browser auth, path confinement, diff/git
resolution, the manifest lock), then W1 domain logic, then the W2 scaffold.
For each: *would you have done it differently, does it hold?* Where you find
a real defect or a design you'd genuinely reverse, **rewrite that specific
file** (review is weaker than authorship for security — rewrite, don't just
nod). Record findings inline here under a "Fable review" heading; confirm or
deny the Codex-fix set (§3 of the out-of-repo HANDOFF, and the RESUME-HERE
list below). Then finish W2.

Out-of-repo reference copies (survive nothing automatically, but exist as
convenience): `/home/denk/work/vocodeck2-workbench-HANDOFF.md`,
`vocodeck2-SPEC-WORKBENCH.snapshot.md`, `vocodeck2-BUILD-WORKBENCH.snapshot.md`.

- **Note:** Fable 5 does **not** use `fab-router` or the `fab-*` skills —
  those are calibration aids for lower-tier models (like the Opus 4.8
  fallback that ran most of this). The earlier RESUME-HERE line owing a
  "fab-router discipline" pass applies only if a *lower-tier* model
  continues; a Fable-native continuation ignores it.

### → Forward policy (user, 2026-07-06)

~~**Defer security-sensitive tasks to avoid doing them under a fallback
model.**~~ *(Superseded 2026-07-06 — see revised policy below.)*

### → Revised policy (user, 2026-07-06)

**Security findings live outside the repo only.** Codex /xai reviews
cover build AND security — but any security issues found are written to
`../vocodeck2-security/`, never into the repo. Fable must not see or
handle security content. The in-repo session (Fable, Opus, whatever
builds) sees only build feedback from /xai reviews. The 11 findings
previously listed here have been moved to `../vocodeck2-security/
deferred-findings.md`.

## Fable review (2026-07-06) — the Opus-authored security surface

Reviewed by a Fable-5-native session per the plan above: §8.5 auth
(`server/http.py`), `confined_read` + page/diff routes
(`server/workbench.py`), diff resolution (`adapters/diffsource.py`),
manifest + lock (`adapters/manifest.py`), plus the supporting store
(`core/workspace.py`), export (`core/review_export.py`), client token
flow (`static/bus.mjs`, `static/app.mjs`), and daemon wiring.

**Verdict: the architecture holds — no file warrants a rewrite.** The
layered design (Origin discipline → per-run wb token → optional bearer;
fd-based confinement; argv-only resolution with shape gates; O_EXCL
lock) is what I would have built. All **11 Codex fixes verified
present** in the code (checked each against its fix site — events-WS
read gate http.py:359, fd-based `confined_read` workbench.py:141,
confined diff_file content used workbench.py:278, O_EXCL lock
manifest.py:69, withdrawn-finding guard workspace.py:554,
`origin/main` kept diffsource.py:63, injection guards diffsource.py:41/83/98,
MAX_DIFF_BYTES workbench.py:264, quoted-path deferral noted diff.py:100,
connect-src 'self' http.py:183). Verified-correct details worth naming:
`Origin: null` is rejected (urlsplit hostname → None), session_ids are
128-bit capabilities, the workbench client already sends `x-voco-wb` on
page fetches, `follow_symlinks=False` on static.

### Deferred security fixes — moved out of repo

11 findings (Codex-verified). See `../vocodeck2-security/deferred-findings.md`.
Not in-repo; Fable and other in-repo models don't see this content.

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
- [x] **W2 — the wake** (complete 2026-07-06): `review` capability
      (CLI + MCP register it), listen returns `{status: "review", items}`,
      items ride `queued` (listen AND live dispatch), at-least-once
      redelivery until status leaves open / ask answered, primary-agent
      election, wake-on-finding/ask. Agent surface: `voco review
      findings|status|reply|export`, `voco page doc|diff`, MCP
      `page_push`/`review_findings`/`review_reply`, discipline text.
      Client: chat dock tab (ask → answer, markdown-rendered), ask events
      in the store. Verified e2e: annotate → parked agent woke → status
      round-trip → ask → reply → restart persistence.
- [x] **W3 — worktrees first-class** (complete 2026-07-06): repo
      grouping shipped in W0 (common_dir); new `adapters/worktree.py`
      (sibling-path `<repo>-<branch-slug>`, ref shape gates, clean-only
      removal — dirty work is never deleted), `voco new --worktree
      BRANCH [--from BASE]`, `session.spawn` worktree spec (creates →
      spawns inside; failed spawn reaps the fresh worktree), kill reaps
      clean / keeps dirty with an honest reason, rail "+" spawns an
      agent in a new worktree. Verified e2e with real git + tmux.
- [x] **W4 — TerminalBackend** (Unix complete 2026-07-06): capability
      cells (`adapters/terminal.py`), asyncio pty backend
      (`adapters/ptyterm.py`: ring buffer, fan-out with drop-oldest
      backpressure, SIGHUP→SIGKILL, VOCO_INSTANCE session link),
      `/v1/term/{session}` WS (replay-first, binary io + resize
      control, browser wb-token gate), terminal pages (pty=stream via
      vendored xterm.js 5.5 + fit; tmux=mirror with peek polling +
      honest say_as_user input row), per-spawn `--backend tmux|pty` +
      `[terminal] default_backend`, pty-aware inject/nudge/peek.
      22 new tests + live e2e. **Windows/ConPTY deferred** — needs a
      Windows machine to validate (pywinpty floor check still pending,
      Phase 0 item 4); the seam is ready for it.
- [x] **W5 — rev/staleness depth** (complete 2026-07-06): interdiff on
      re-push (`core/interdiff.py`, ported from the oracle — per-file
      hunk-hash identity; removed counts as touched), export stamps
      `area_changed` on stale diff anchors (legacy array stays
      five-field), since-rev banner + per-file inter-diff chips + stale
      finding chips in the client, live-git tracker (daemon interval
      re-resolve from the recorded source; conservative on transient/
      empty git states; `[workbench] live_git_s`, `workspace.live`
      command for per-workspace opt-out). Oracle re-review scenario
      ported as tests; verified live incl. manifest-restored pages.

## RESUME HERE (updated 2026-07-06, Fable-native session — W2 shipped)

- Security findings live in `../vocodeck2-security/` ONLY. Codex /xai
  reviews cover build + security, but security issues route to the
  parent-dir files only — never into the repo, never into the session
  reply. In-repo models don't read security content.
- **ALL PLANNED SLICES (W0–W5) ARE DONE AND VERIFIED** (W4 Unix-only;
  see milestones + journal). Gates: 291 pytest, mypy (49 files), ruff +
  format, tsc --checkJs, PROTOCOL.md regen-clean (30 events, 24
  commands). Codex build review of W2 applied (6/6 fixed + tested).
- Remaining (post-slice) work, in rough order:
  1. Live browser click-through of the whole workbench (user AM — no
     headless browser in this env; every surface verified at HTTP/WS
     level).
  2. README refresh (workbench section: `/`, voco review/page/new
     flags, config keys) + a Codex /xai build review of W3–W5.
  3. Windows/ConPTY for W4 (needs a Windows machine; pywinpty floor
     check pending — Phase 0 item 4).
  4. Ideas parked: per-workspace primary selector UI (command exists:
     `review.primary`), pty terminals in the pane watcher, CodeMirror
     file-viewer page.
- Review policy (user): reviews — Codex or self — are on the BUILD
  itself. No security side-channel orchestration in this track.
- Env note: `~/.local/bin/node` is a self-looping symlink the user still
  needs to `rm`; use `PATH="$HOME/.nvm/versions/node/v24.11.1/bin:$PATH"`
  for tsc until then.

## W2 build order — **COMPLETE 2026-07-06** (Fable-native)

1. [x] `review` capability gates the wake; CLI + MCP register it.
2. [x] `listen` returns `{status: "review", items}`; items ride `queued`
       on listen AND live dispatch; at-least-once until status leaves
       open / ask answered; idempotent by item id; no turn_id. Item
       shape: `{kind: finding|ask, id, workspace, finding|ask}` (nested
       payload — item keys can't collide with domain keys; the scaffold's
       flat spread let finding.kind clobber the discriminator).
3. [x] Ask/chat: `ask.create` → primary election → `ask_reply` →
       renders under the chat/finding card (markdown, sanitized).
       Answering an OPEN question-kind finding auto-addresses it
       (the reply IS the round-trip; redelivery must converge).
4. [x] MCP tools `page_push`/`review_findings`/`review_reply`; CLI
       `voco review findings|status|reply|export` + `voco page doc|diff`
       (export as a real CLI verb landed HERE — the W1 journal
       overstated it; only the control command existed).
5. [x] Discipline text: MCP INSTRUCTIONS paragraph + voice_init rule.
6. [x] Client: chat dock tab (pending count, honest no-agent note),
       ask events in store, finding.answer markdown-rendered.
7. [x] W2 exit verified e2e (real daemon, real repo): annotate → parked
       agent woke → `voco review status` → ask → reply → export →
       restart → pages/findings/asks all restored; missed-wake
       redelivery covered by tests.

## Journal

- **2026-07-06 (W5 shipped — all planned slices done)** — Re-review
  depth, oracle-exact: `compute_interdiff` ports interdiff.mjs (hunk
  hashes, not line heuristics), `upsert_diff` records it on every
  re-push, export stamps `area_changed` (snake_case — SPEC naming — on
  our sidecar; the five-field legacy array untouched), diff view gets
  the since-rev banner + per-file chips, findings dock gets stale
  chips. Live-git: the daemon re-resolves recorded sources on an
  interval (default 5s; `live_git_s = 0` off; `workspace.live` per-ws)
  and upserts exactly like a re-push. The oracle's rereview.test.mjs
  scenario ports 1:1 (8 tests). Live e2e caught the best possible
  outcome: a page RESTORED from manifest (rev 3, prior run) was
  live-tracked to rev 4 with correct interdiff and the finding
  exported stale+area_changed — persistence and tracking compose.
  291 tests, all gates green. **W0–W5 complete.**
- **2026-07-06 (W4 shipped, Unix)** — TerminalBackend native. The pty
  backend is asyncio-first (loop.add_reader, no reader threads); the
  ring buffer — not client queues — is the recovery source (reconnect
  replays; stalled clients drop-oldest). Session↔terminal link is env
  (`VOCO_INSTANCE=pty-N` baked at spawn; `derive_identity` prefers it),
  so register-time wiring is pure data: terminal page + cells derive
  from identity. Registry stays transport-free via an injected
  `term_cells` hook (same pattern as review_items). tmux keeps its
  superpowers (restart survival, native attach) as a mirror-mode page.
  Live e2e: spawn pty → register → `term·Orion` page + cells → WS
  replay → typed round-trip → kill. Windows/ConPTY: seam ready,
  validation deferred to a Windows machine. 283 tests, all gates green.
- **2026-07-06 (Codex W2 build review applied)** — /xai adversarial
  review of the W2 diff (security → out-of-repo side file per policy;
  Codex's first attempt couldn't write it — sandbox — so a scoped
  re-run routed it; response carried build feedback only). 6 findings,
  all real, all fixed + tested:
  1. BLOCKER — agent-scoped page findings now wake THAT page's agent
     (§4.3): pending items carry `agent: call_name`; `_review_items_for`
     delivers own-page items always, workspace items only to the
     primary; `_wake_target` routes the wake. Departed owner → nobody
     woken, ledger keeps the item.
  2. WARNING — election rule 3 now prefers PARKED sessions (most
     recently parked wins; merely recently-seen can't be woken).
  3. WARNING — review wakes mark the session `reviewing` → state reads
     "working" (no idle-nudge on mid-review voice input; honest dots).
     Ephemeral, cleared on next listen, never persisted.
  4. WARNING — exact-duplicate `ask_reply`/`finding_status` replays are
     true no-ops (no event, no ts bump) — at-least-once converges;
     different writes still last-writer-win per §4.1.
  5. WARNING — chat dock's "no agent attached" note is now
     workspace-scoped: session snapshot + session.attached carry
     capabilities/host/root; unknowable place counts in the agent's
     favor.
  6. NOTE — `review.primary` control command (§4.3 override): pin/clear
     a workspace's primary; stale override self-drops. In-memory.
  Also: PROTOCOL.md regenerated (23 commands).
- **2026-07-06 (W3 shipped)** — Worktrees first-class, native. New
  `adapters/worktree.py` (same injected-Runner shape as tmux so daemon
  tests fake both with one recorder; `git -C`, no cwd juggling;
  `valid_ref` now shared from diffsource). Daemon: `session.spawn`
  gains `worktree: {branch, from}` (local-only, worktree created in
  executor then spawned into; spawn failure reaps the fresh tree),
  `session.kill` reaps clean worktrees this run created — dirty ones
  are kept and say why; the map is in-memory BY DESIGN (post-restart
  not-knowing fails safe: no removal). CLI `voco new --worktree/--from`;
  rail "+" per repo group. 13 new tests (250 total). E2E with real
  git+tmux: spawn → worktree created + session inside it; dirty kill
  kept the tree; clean kill removed it; CLI path verified. Gotcha
  caught live: a stale pre-W3 daemon still held the port and silently
  served the first spawn attempt — the new daemon's bind refusal +
  lock refusal surfaced it exactly as designed.

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
- **2026-07-06 (Fable W1 build review + W2 shipped)** — Fable-native
  session took over at `c31041a` per the no-rewind plan. Reviewed the
  W1/W2-scaffold code (current code only, build quality): **holds; no
  rewrite warranted** — pure store + injected edges + shared command seam
  is the right architecture. Three real defects found + fixed: (1)
  `restore_workspace` partial failure orphaned `_pages_by_id` entries
  (now registers only on full success); (2) primary election used
  mutating `resolve()` on read paths — new non-mutating
  `WorkspaceStore.home_of()` (election can't mint sessionspaces or dirty
  manifests); (3) `pending_review()` item spread let `finding.kind`
  clobber the `kind: finding` discriminator — items now nest their
  payload. Then built W2 complete (tests first: 16 wake tests + 14
  adapter tests; build order above). Adhoc: config schema knows
  `[server]`/`[workbench]` (validator warned on real sections);
  `dispatch()` now attaches review items to `queued` (spec says ALWAYS
  ride along); `answer_finding` auto-addresses open questions. E2E on a
  real daemon+repo passed incl. restart persistence; single-writer lock
  incidentally validated live (a stray boot got refused). 237 tests,
  all gates green. Next: /xai build review of W2, then W3.
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
