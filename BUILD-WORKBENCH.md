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

**Defer security-sensitive tasks to avoid doing them under a fallback
model.** Fallback switches are silent and drop the work into a lower tier
mid-task; security code (auth, confinement, injection surfaces, crypto,
lock/atomicity) must not be authored by an unintended fallback model.
When security work is next, either confirm the session is on the intended
model first, or hold the task until it can be. The §8.5 auth here was
authored under exactly the fallback we now want to avoid — hence the
Fable review request above.

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

### Deferred security fixes (found in review; NOT implemented — policy:
### no security authoring while silent model fallback is possible)

Ordered by severity. Each is specified so any confirmed-on-model session
can implement without re-deriving.

1. **`GET /v1/page/{page_id}` is completely ungated** (workbench.py
   `page_content`) — no `_check_origin`, no `_check_auth`, no wb check,
   and page ids are sequential (`pg-1`, `pg-2`…). On a bearer-configured
   shared host any local user can read proprietary doc/diff/screen
   content, and the response leaks `session_id`s (page.meta()), which
   are capability tokens (chains into listen-stealing). Inconsistent
   with Codex BLOCKER 1's own rationale: the snapshot (metadata) is
   gated on browser WS reads while full page *content* is open. FIX:
   `_check_origin` + `_check_auth` + wb-required-for-browser-origin
   (same shape as `_events_ws`). Client compat: `app.mjs` already sends
   `x-voco-wb`; the debug UI does not fetch `/v1/page`. Add tests:
   foreign origin 403, loopback-origin-no-wb 403, no-Origin+bearer ok.
2. **No `frame-ancestors` in the CSP** (http.py `html_response`) —
   `default-src` does NOT cover framing, so a hostile page can iframe
   `http://127.0.0.1:7777/` and clickjack the workbench (finding
   mutations now; typed terminal input at W4). FIX: append
   `; frame-ancestors 'none'` to the CSP and set
   `X-Frame-Options: DENY` on `html_response`.
3. **Manifest lock takeover race** (manifest.py `acquire`) — takeover
   unlinks a stale lock by *path*: daemon A can pass the liveness check,
   then unlink the FRESH lock daemon B just created in the window, and
   both win. FIX (keeps the spec's O_EXCL+nonce design): atomic-rename
   takeover — `os.rename(lock, lock.with-suffix(f".takeover-{pid}")`;
   only one renamer wins; winner unlinks the renamed file and loops to
   the O_EXCL create; `FileNotFoundError` on rename → lost the race →
   loop. (Alternative architecture if ever revisited on-model: `flock`
   on a persistent file — kernel-exclusive, auto-released on death,
   deletes the whole pid/nonce machinery; POSIX-only, so it needs the
   current scheme as the Windows fallback. Not required — the rename fix
   is sufficient and spec-shaped.)
4. **Data-dir/file permission gaps vs spec §8** ("data dir 0700, files
   0600"): (a) `manifest.acquire`/`save` `mkdir` without `mode=0o700`
   (default 0755-umask) — dir listing leaks workspace keys = repo paths;
   (b) `review_export._atomic_write` writes tmp with default perms
   (0644-umask) and chmods 0600 only AFTER `os.replace` — a
   world-readable window for proprietary review JSON on shared hosts.
   FIX: `mkdir(mode=0o700)` (+ one-time `chmod` of pre-existing dirs at
   acquire), and write exports via `os.open(..., 0o600)` like
   `manifest.save` (drop the after-the-fact chmod).
5. **`confined_read` residual symlink race** (workbench.py) —
   `O_NOFOLLOW` covers only the FINAL component; an intermediate dir
   swapped to a symlink between `resolve()` and `open()` escapes root
   once. Attacker needs write access inside the workspace root, so low.
   FIX (portable, ~6 lines): after `fstat(fd)`, re-`resolve()` the base
   path and re-check containment, and compare `(st_dev, st_ino)` of
   `os.lstat(re-resolved)` against the fd's fstat — mismatch → 404.
   (Hard links inside root remain readable — inherent to any
   path-confinement, not a defect. openat2/RESOLVE_BENEATH or a
   dir_fd component walk is the exact fix if ever needed; Linux-only.)
6. **Timing-unsafe token comparisons** (http.py:129/133/157) — bearer
   and wb tokens compared with `==`. FIX: `secrets.compare_digest`
   (guard the None/str types first). Low (loopback + HTTP jitter), cheap.
7. **`DiffResolver.resolve` still carries an unconfined `diff_file`
   open** (diffsource.py:104) — dead code from the route (BLOCKER 3
   fixed at the call site), but a future caller re-opens the hole
   silently. FIX: delete the branch; raise
   `DiffResolveError("diff_file is resolved by the caller (confined)")`.
8. **Resolved diffs are uncapped + subprocess timeout unmapped**
   (diffsource.py) — `MAX_DIFF_BYTES` caps only *pasted* content; a
   giant `git diff`/`gh pr diff` output OOMs the daemon
   (`capture_output` buffers all of it), and `subprocess.TimeoutExpired`
   escapes as a raw 500. FIX: length-check resolver output against
   MAX_DIFF_BYTES (raise DiffResolveError naming the cap), catch
   TimeoutExpired → DiffResolveError.
9. **Manifest save lacks fsync** (manifest.py `save`) — crash after
   `replace` but before writeback can land an empty/truncated
   manifest; `load_all` then silently skips it = lost review data.
   FIX: `fh.flush()` + `os.fsync(fd)` before the replace. (Durability,
   not confidentiality — kept on this list because it's the same file.)

Notes, no action needed: wb token rides `?wb=` on the WS URL (spec said
subprotocol; query is fine on loopback — aiohttp doesn't log URLs by
default, WS URLs don't enter history); bearer accepted via `?token=` on
non-WS routes too (same reasoning); binary sniff checks only the first
8 KiB (rendering nuisance at worst); `html_response` replaces
`{{nonce}}` body-wide — fine while only server-authored HTML flows
through it, worth an invariant comment when next edited on-model.

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

## RESUME HERE (updated 2026-07-06, Fable session)

- Security review of the Opus-authored surface: **DONE** (see "Fable
  review" above). Architecture confirmed; 9 fixes deferred to that list
  per the security-defer policy (user, 2026-07-06: note, don't author).
- Now executing: **W2 (the wake)** — tests + MCP tools + client panel +
  discipline text (list below). Then /xai review of the W2 diff, then
  W3. Commit + push at milestone edges; journal mid-milestone.

State of the checkpoint captured by commit `c6aa94d`:

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
- [ ] Review discipline on the W2 diff before its "done" commit:
      trust-boundary sink pass + adversarial self-review. (If a
      LOWER-TIER model continues, `fab-*` skills are the aid; a
      Fable-native session uses its own judgment — see MODEL PROVENANCE.)

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
