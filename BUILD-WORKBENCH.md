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

## RESUME HERE (updated 2026-07-06 night — PROPOSAL OUT, AWAITING THE YES)

**State: the protocol-reliability fix is SHIPPED (`5a43f29`); the UI/UX
re-architecture is at REV 4 — FINAL (four review rounds: 40% → 50-60% →
80% → agreed final scope) and awaits only the user's overall yes.**
`DESIGN-DECK.md` is the pinned spec (design system rules, zones,
policies, scoping, signal map, daemon budget, U0–U3); the interactive
mockup is `DESIGN-DECK.mockup.html` / the artifact link inside. All
four forks are user-decided; naming, workspace demotion, quiet-skin
rules, disconnected state, destructive/toast/a11y/keyboard policies are
all pinned. **On the yes: U0 starts** — `page.publish`,
`workspace.open`, speech who+text + `speech.sentence`, per-session
user-input log — tests at the command seam, no pixels until U0 is
green. The mandate text below stands.

## Previous RESUME (2026-07-06 EOD — UI/UX RE-ARCHITECTURE MANDATE)

**Read this section fully before writing any code. The machinery below
(W0–W5) is built and solid; the PRODUCT on top of it failed its first
real dogfood. The next session's job is a design-first UI/UX
re-architecture plus one protocol-reliability fix, in that order of
thought but reverse order of build.**

### Field report (user dogfood, 2026-07-06 evening — verbatim distilled)

1. **Voice has no visual presence.** Speaking produces NOTHING on
   screen (no mic-live/VAD indicator, no partial transcript against the
   speaking moment); an agent speaking back shows nothing either. For a
   VOICE control plane this is the soul of the product and it is
   absent. The `stt.partial` ticker in the status bar is not presence.
2. **"Feels like a debugger, not a daily tool"** — for the owner, a
   friend, or a GitHub rando. No product/UI engineering practices.
3. **Less useful than the diff-annotate reference project**: there the
   user could SEE diffs against branches in one step. Here the diff
   never appeared (see protocol failure below) and there is no
   user-side "show me the branch diff" affordance at all — publishing
   is agent-initiated only. The reference bar for the review surface is
   diff-annotate's UX, and we are under it.
4. **Two chats confusion**: a dock "chat" tab AND a bottom-strip input.
   Transport semantics (ask vs say_as_user vs transcript vs say) leak
   into the UI as separate surfaces. Must become one coherent story.
5. **Panels are not resizable.** Table stakes.
6. **REJECTED design direction** (do not revive): messenger-first /
   chat-app layout — "this is not a messenger tool, otherwise we would
   just use the terminal." The conversation is not the center; the
   WORK is. Voice presence + agent state + artifacts are the product.

### Protocol failure from the same dogfood — **FIXED 2026-07-06 (next session)**

All three fix classes below landed + verified e2e (see journal:
"protocol reliability"): (a) identity rides every workspace verb and the
daemon re-keys the session to it; (b) every bridge 4xx names the
resolved root + attempted path; (c) no-bare-500 middleware; plus the
client session cache now keys on the FULL cwd (the two-checkouts-one-
basename collision was reproduced and killed). Original report kept
below for history.

### Protocol failure from the same dogfood (root-caused; historical)

Agent (MCP, cwd `/home/denk/vocodeck2` — note: a THIRD checkout) tried
`page_push`:
- `{"branch": ""}` and `{"branch": "origin/main"}` → **500**.
- `diff.file` in /tmp → "outside workspace root" (correct), copied into
  the repo → **"no such doc"**; `path: "README.md"` → **"no such doc"**
  (the file plainly exists in the agent's cwd).

Diagnosis: the resolver is EXONERATED — reproduced its paths green
against an origin/main-only repo (no local main). The evidence pattern
(500 on git sources + not-found on files that exist + confinement
refusals) all matches ONE cause: **the server-side session carried a
stale workspace root** — identity is captured at register and kept by
the client session cache / state restore; every workspace verb then
resolves against the wrong root. `git` run in a dead cwd raises
unhandled (FileNotFoundError → bare 500 on builds before `b8a82df`).
Fix class for next session:
  a) **Refresh identity per bridge call** (adapters send current
     cwd/worktree with page/findings verbs; server re-resolves) or
     equivalent staleness kill;
  b) **Errors must carry context** — echo the resolved workspace root
     + attempted path in every 4xx (the agent flailed through six
     blind attempts on "no such doc");
  c) **No bare 500s** on the bridge — catch-all → 4xx/5xx with a
     message body.

### The mandate (agreed with user before session end)

- Dogfood the WEB UI first; Tauri comes later. (v1 at
  `~/code/vocodeck-p` is Tauri 2 + React + Vite with a Vd* component
  library and global-PTT plugin — the eventual product shell; port map
  deferred until the web UI earns daily use.)
- **Design first, code second**: next session produces a real UX
  re-audit and proposes the information architecture + mockups to the
  user BEFORE building. The last two UI passes were built blind and
  failed the first touch. Non-negotiables gathered so far: voice
  presence (mic capture live, agent speaking live, per-agent state),
  user-initiated branch-diff review (diff-annotate parity: pick a
  branch, see the diff, no agent required), ONE input story, resizable
  panels, product-grade empty/error states.
- Protocol reliability fixes (a–c above) land WITH the redesign — the
  best UI dies if page push 500s.

## Previous RESUME (W2-era, kept for history)

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

- **2026-07-07 (design rev 4.1 — the quiet pass overshot; semantics
  restored)** — User verdict on rev 4: "reverted to a prototype looking
  interface, we didn't need to simplify THIS much" + "full doesn't
  actually expand anything". Root cause: the quiet pass stripped
  MEANING along with decoration. Two rules amended in the design
  system: (1) semantic color is never stripped — diff +/− green/red
  everywhere (file heads, rail sub-tree, header totals), agent state
  words tint like their dots, needs-you counts amber; (2) controls
  carry a 1px edge — buttons/inputs get borders back (stop/interrupt
  read as danger controls), only layout/list separation stays
  borderless. Demo fix: routed scenario now starts the dock on
  annotations so pressing "full" VISIBLY switches to the transcript and
  flashes the entry (flash animation, restartable). Substrate question
  answered and recorded in DESIGN-DECK.md: vanilla .mjs, no framework
  (SPEC decision 3 reaffirmed) — Tauri is a webview shell so the client
  ports unchanged; native bits (global PTT, tray, notifications) are
  framework-independent; htm+Preact stays the per-panel escape hatch.
  Artifact re-published, same URL. Awaiting the yes.
- **2026-07-07 (design rev 4 — FINAL: the quiet pass)** — Scope agreed
  in discussion, then built in one pass. User's diagnosis ("very busy,
  nothing like Zed") answered with countable fixes: borders → four
  structural edges + background tiers everywhere else; nine font sizes
  → three type slots (13 sans chrome / 12.5 mono content / 11 micro);
  lowercase chrome (uppercase shouting was the loudest single choice);
  color budget = monochrome at rest (amber only voice-live/needs-you,
  blue only selection, red only blocked/destructive). User's overlap
  catch (both utterance cards colliding in Routed) fixed by KILLING the
  floating-card pattern: "full" now jumps to the highlighted transcript
  entry — symmetric for user/agent, enlargeable, zero floating layers;
  agent karaoke lives in the entry. Best-practices gaps closed as
  design: disconnected state (new scenario: reconnecting strip, dim
  read-only surfaces, persistent error toast), destructive policy
  (undo-over-confirm; confirm only for kill), toast policy (errors
  persist, successes fade), a11y floor (aria-live captions/transcript,
  no color-only state, ink3 never sets words, focus rings), keyboard
  floor as named U1 tasks, and the design system CODIFIED as a rules
  block that lands atop styles.css in U1. DESIGN-DECK.md rewritten as
  the pinned spec (changelog stays in git). Mockup validated (tag
  balance + JS id refs), synced, artifact re-published same URL.
  AWAITING THE YES → U0.
- **2026-07-07 (design rev 3.2 — no-MORE/no-LESS audit; user at 80%)** —
  User asked for a minimalism/completeness audit ("edging towards too
  many things"), the annotations rename, page-type icons, and a
  supportability check on the karaoke captions. HONESTY FIX: verified
  against code that a say is ONE PlaybackItem (sentences chunked inside
  `_sentence_synth`) — `speech.started` fires per MESSAGE, so per-
  sentence karaoke needs a new U0 item: `speech.sentence` emitted from
  the generator at sentence pull (playback-aligned; degrade = whole-
  message highlight). Corrected in both docs. Audit CUTS: orb "full
  duplex" sub-label, repo-group counts, review-only explainer row,
  "exported hh:mm" status cell, ◆ pinned marker. ADDS: ✕ withdraw on
  open annotations (finding.withdraw had no UI), page-row ✕ close on
  hover (U1 note). PARKED: OS notifications on blocked (herdr has
  them), Ctrl+P, custom groups, transcript search. Naming resolved:
  dock tab ANNOTATIONS (lists things), repo button stays REVIEW (starts
  an activity). Orange tree bar → page-type icons (◈▦±¶❯). herdr status
  tracking checked: process-name + output heuristics only — our bridge
  facts are strictly stronger; borrowed their presentation (state
  counts, blocked-first sort). Artifact re-published, same URL.
- **2026-07-06 (design rev 3.1 — agent live captions, one REVIEW home,
  herdr borrows)** — Three more user notes folded in: (1) the speaking
  agent now gets the same live-caption treatment the user gets —
  current sentence in the strip, FULL ⌄ card with the whole response
  karaoke-highlighted (dim said / highlight hearing / faint queued),
  STOP cuts the rest; drivable today (sentence-wise TTS + agent.say +
  enriched speech.started). (2) REVIEW button de-duplicated: repo group
  header ONLY (work-header ⊕ removed) — one concept, one home. (3)
  herdr reviewed (README only, concepts only per AGPL/SPEC decision 7):
  adopted state-count aggregation in the status line and blocked-is-
  loud attention-first rail ordering (U1); real terminals/detach-
  survival/scriptable API we already have; its arrangement model stays
  parked. Confirmed to the user worktrees are fully supported (W3).
  Artifact re-published, same URL. Rev tracking: rev 3 targeted the
  70-80% ask.
- **2026-07-06 (design rev 3 — brutalism, transcript, rail tree; user at
  "50-60%", aiming 70-80%)** — Next redline batch, all folded into
  DESIGN-DECK.md rev 3 + mockup: tech-brutalist skin (zero
  border-radius except the mic orb — "the only curve in the deck is the
  voice"); "chat" renamed TRANSCRIPT and rebuilt as a radio log (no
  bubbles/alternation — kills the messenger feel the user flagged);
  diffs are a collapsed-by-default file index (per-file fold + EXPAND
  ALL; finding/since-rev files auto-expand — new fork 4); center tabs
  KILLED in favor of pages nested under agents in the rail (one
  navigation axis; agent-name prefixes drop structurally — answered the
  user's "is selected-agent-per-page right?" with yes); workspace
  demoted to data-only, rail groups agents BY REPO via common_dir
  (agentless groups render "review-only"; custom groups parked — SPEC
  decision 20); bottom status line restored (ambient truth only;
  presence strip keeps voice moments); terminal page view added (pty
  stream, ring-replay, focus tag, kill; tmux mirror noted); first-run
  de-CLI'd — Spawn an agent (surfaces existing session.spawn) / Review
  a diff / Open a repo, commands demoted to a "connect →" modal. Daemon
  budget unchanged from rev 2 (input log, page.publish, workspace.open,
  speech who+text); spawn/connect need nothing new. Artifact
  re-published same URL. STILL NOTHING BUILT — awaiting yes + four
  forks (orb, input placement, transcript depth, collapse default).
- **2026-07-06 (design rev 2 — user redlines applied; still awaiting the
  yes)** — User verdict on rev 1: "good start, 40% there", six
  redlines. All addressed in DESIGN-DECK.md rev 2 + reworked mockup:
  (1) long routed prompts → one-line strip + "⌄ full" expansion card,
  full text in chat; (2) weak history popover → KILLED, history is now
  the Chat tab in the right dock (user's suggestion), per-agent,
  wrapped, restart-surviving; (3) review panel mixing agents/repos →
  dock is scoped to the selection with a scope header (Freya ·
  vocodeck2 · workbench); (4) right panel tabs → Review | Chat; (5)
  annotation input → adopted VERBATIM from the diff-annotate reference
  (diff-panel.mjs inline editor + finding-controls.mjs pills
  Concern|Question|Nit + blocking + tip line); (6) no settings → gear →
  settings modal over config.get/set, honest hot-apply vs RESTART
  marks. Scoping semantics pinned per the user's "one agent → one
  surface" direction (table in DESIGN-DECK.md); voice moments are the
  single global exception. His "route and queued in chat — daemon
  better support that" concern answered by keeping route/moment data
  OUT of chat (strip-only, dies with the moment) and adding exactly one
  daemon piece: a per-session user-input log symmetric to the existing
  say_log (deque(50), persisted, recorded at dispatch()). U0 now also
  carries that log. Artifact re-published (login invalidated the rev-1
  URL): https://claude.ai/code/artifact/29d18572-3742-46d6-bb7f-4e3c6d9cc7d0
  STILL NOTHING BUILT — awaiting yes + fork answers (orb interaction,
  input placement, chat depth).
- **2026-07-06 (design proposal delivered — DESIGN-DECK.md + interactive
  mockup)** — The design-first half of the mandate. Full client audit
  first (every field-report item traced to its code cause: no
  turn.state/speech.* subscriptions; stt.partial declared but UNEMITTED
  — batch whisper; §3.2's human diff picker speced but never built; two
  inputs with different semantics; fixed 240/1fr/320 grid). Proposal:
  presence strip (orb + live captions + ONE input + agent-speaking slot;
  status bar and feed strip die), rail agents+repos with standing
  Review buttons, review picker (branch/PR/staged, agentless), findings
  + asks merged into one ledger (chat tab dies), history as a summoned
  drawer (never docked — messenger-first stays rejected), draggable
  persisted panels. Every presence visual is bound to an EXISTING
  protocol signal (map in DESIGN-DECK.md); the one honest gap
  (stt.partial) is labeled, the UI subscribes anyway. Three underlying-
  logic additions proposed: `page.publish` + `workspace.open` control
  commands (agentless review parity) and speech.started/finished
  payload enrichment (who + text). Build order U0–U3, each ending at a
  user click-through. Mockup is interactive (scenario states, working
  panel-drag, one-input demo): DESIGN-DECK.mockup.html, published as an
  artifact too. NOTHING BUILT — awaiting the user's yes + three fork
  answers (orb interaction, input placement, drawer scope).
- **2026-07-06 (protocol reliability shipped — the dogfood 500s/no-such-doc
  class is dead)** — Fable-native session, per the mandate's build order
  (protocol first, UI only after an approved design). Three fixes + one
  root-cause kill, all with tests: (a) **identity rides every workspace
  verb** — `identity` in POST bodies / JSON query param on GETs for
  page/findings/finding_status/ask_reply/say/screen/listen; new
  `Registry.refresh_identity` re-keys the session (same session_id, new
  identity map key) and emits `session.attached` (an upsert client-side)
  when the home root moves; SPEC §3.2 updated + decision 27. (b)
  **contextual 4xx** — `confined_read` names attempted path + resolved
  root; sessionspace/remote/diff/finding errors name cwd/root/ids. (c)
  **no bare 500s** — aiohttp middleware wraps unexpected exceptions in
  `{ok:false, error:"Type: msg"}`; dead workspace roots get a named
  runner error instead of masquerading as "git: not installed". Root
  cause confirmed and killed: the CLI session cache keyed on cwd
  BASENAME — `/a/proj` and `/b/proj` shared a session; now full-cwd
  hash. `page_push` replies + CLI/MCP confirmations now name the
  workspace root the page landed in (agents see WHERE). Verified e2e on
  an isolated headless daemon: stale-root relative push → 404 naming
  both path and root; same push with fresh identity → lands in the live
  root; dead-root branch diff → clean 400 naming the root; two
  same-basename checkouts → two sessions, two workspaces, via the real
  CLI. 316 pytest, mypy, ruff+format, tsc, PROTOCOL.md all green.
  (E2E hygiene note: `--config` with `[state] dir`/`[workbench]
  data_dir` pointed at scratch — first boot attempt accidentally
  restored the real `~/.local/state/voco` sessions; killed within
  seconds, state untouched beyond a re-save of what it restored.)
- **2026-07-06 (EOD — dogfood verdict: UI fails daily use; re-architecture
  mandated)** — The user drove the workbench for real. Verdict: still a
  debugger, not a product. Full field report + root-caused protocol
  failure (stale session workspace root → 500s and "no such doc" on
  real files) + the design mandate live in RESUME HERE at the top of
  this file. Messenger-first concept was proposed and REJECTED; voice
  presence and diff-annotate-parity review are the named bar. Session
  ended here by user decision; next session starts with the UX
  re-audit + IA proposal, user-approved before code. Also fixed the
  "typo": one wwaged interrupt mid-session produced the verdict that
  UI passes must end in user click-throughs, not green gates — the
  discipline that produced tonight's honest verdict.
- **2026-07-06 (product pass — agent-centric UI, user-driven redesign)** —
  Live use verdict: "worse debug page, unusable". Audit traced four
  roots: (1) resumed harness sessions mint a NEW identity (instance =
  harness session id) — the old session lingered as a grey corpse and
  often still held voice-active, hijacking ask routing; (2) election
  rule 1 trusted "active" with no liveness check; (3) the operator
  affordances (activate/detach/mic controls/transcript log/say box)
  never made it over from the debug UI; (4) empty states assumed pages
  exist. User set the model (his words): "select one agent: that agent
  is active, plus its pages, plus its chat, its findings." Built exactly
  that: ONE selection — clicking an agent activates voice, opens its
  pages (own screen first, else an agent CARD: state, last says, screen
  preview, command hints), pins chat (review.primary). Rail is
  agents-first with ✕ detach + unread/queued chips; workspaces are a
  secondary browse list. Bottom voice-feed strip (you/route/say/queued
  lines + type-as-user box, collapsible) — ported from the proven debug
  UI pattern rather than invented. Status bar interactive (duplex,
  attention, ■ interrupt). Lifecycle: predecessor sweep (unparked-idle
  sibling > 60s silent, or working > 15 min, same host+cwd+harness) —
  the newcomer inherits voice-active; election skips unreachable
  actives; re-register refreshes capabilities. Findings stay one ledger
  per checkout (deliberate). 306 tests green. VERIFICATION DISCIPLINE
  CHANGE: UI passes now stop for a user click-through before further UI
  work — gates green ≠ usable was this pass's lesson.
- **2026-07-06 (live Windows report: agents invisible — fixed)** — User
  ran the workbench on the Windows profile: agents registered but the
  rail showed them as offline with no workspaces. THREE bugs, two of
  them cross-platform and masked by Linux e2e (which always pushed a
  page first): (1) workspaces were only minted on page push — a
  register-only agent belonged to nowhere; register + boot-restore now
  resolve the workspace immediately, and the client groups agents by
  home identity (host/root) instead of only agent-scoped pages.
  (2) `display_state()` (§6) existed but was NEVER WIRED — snapshots
  carried raw "parked", the CSS has no `.dot.parked`, so listening
  agents rendered as unstyled/offline dots. Now derived server-side and
  carried on snapshot + session.state + pane.hint; roster rows show
  state + unread. (3) Windows: the lock's raw `os.open` flags failed
  ([WinError 11] per the user's research agent), disabling persistence
  → portable `open("x")` + best-effort chmod; ALSO found `_pid_alive`
  used `os.kill(pid, 0)`, which on Windows is TerminateProcess — the
  liveness probe would have KILLED the lock holder — now a
  ctypes/OpenProcess query; manifest saves portable; `--backend pty`
  on win32 gets a clean error (ConPTY still pending). 300 tests green.
  Remaining Windows gap: terminal pages need tmux (WSL2) or the future
  ConPTY backend; everything else works natively now.
- **2026-07-06 (Codex W3–W5 build review + Fable self-review pass 2
  applied)** — Codex: 7 findings. Fixed (with tests): (1) BLOCKER —
  dirty worktrees were unreclaimable: kill killed the session first, so
  a retry died before the reap; kill failure with a PENDING worktree
  now proceeds to reap (session honestly reported "already gone"); the
  test fake now fails double-kills like real tmux. (3) /v1/term
  replay/subscribe race → subscribe+snapshot are now atomic
  (back-to-back, no await): no lost or duplicated frames on attach.
  (4) naturally-exited ptys deregister from the backend (404, not a
  dead stream). (5) resolved/live diffs now respect MAX_DIFF_BYTES
  (route 413s; live tracker disables itself for that workspace and
  says why). (6) missing git/tmux/gh binaries surface as clean
  adapter errors, not FileNotFoundError. Deferred: (2) Windows/ConPTY
  (needs Windows; already documented). DECLINED: (7) "unify tmux+pty
  behind one Protocol" — deliberate design: capability CELLS drive
  consumers, and the backends genuinely differ in execution model
  (sync subprocess vs loop-native); a forced common interface would
  abstract without payoff. Self-review pass 2 additionally fixed: pty
  kill blocking the event loop (akill waits in the executor), snapshot
  reconnects now invalidate findings/asks caches, watcher is
  backend-agnostic (injected pty capture + ANSI strip so classify
  reads raw streams). 296 tests, all gates green.
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
