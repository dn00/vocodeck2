# VocoDeck 2 Workbench — Pages, Review Surfaces, Terminal Backends, Worktrees

Status: **v1.0** (2026-07-06) — adversarially reviewed (Codex, 40
findings; ~36 verified + applied, 1 rejected as a misread) and grilled
with the user (7 decisions, §13 entries 20–26). Ready for build; the
build order lives in BUILD-WORKBENCH.md. Companion to [SPEC.md](SPEC.md)
(the foundation spec); this document specifies the browser workbench that
fills SPEC §2's "future UI (WS)" slot and absorbs the review-cockpit
capabilities of the **diff-annotate** skill
(`claude-setup/skills/diff-annotate`) into one packaged product.

Version boundary: the foundation spec's v1 platform claims (e.g. §9.2
"native-Windows harnesses are attach-only in v1") describe the
foundation milestones M0–M3. This workbench spec post-dates them and
amends them where stated (W4 amends §9.2).

Reference implementation (read before building; it is the oracle, not a
dependency):

- **diff-annotate** — ~4.6k lines of buildless browser client (panel
  contract, store, typed bus, diff table, findings cards) that ports
  nearly intact, and ~4.4k lines of Node server domain logic (workspaces,
  subjects/revs, findings ledger, inter-diff, confinement) that is
  **rewritten in Python against its test suite** (17 test files — port
  the tests first, per slice). Its `DESIGN.md` "known follow-ups" and
  security sections are a checklist of mistakes not to re-make.
- **herdr** (github.com/ogulcancelik/herdr) — concepts only, it is AGPL
  (precedent: the `pane_state.py` docstring). We take: real-PTY-per-agent,
  sidebar state dots, detach/reattach discipline. We do not take code or
  depend on the binary.

---

## 1. What this is

One clean package: the voco daemon grows a **browser workbench** — a
Zed-style panel UI served from the daemon's existing port — where each
**workspace** (a checkout; worktree-aware) holds a set of typed
**pages**: the agent's screen, its terminal, a reviewable diff, pushed
docs. Humans annotate any page; annotations reach agents through the
same wake channel as speech. Voice remains the flagship input, but the
workbench must be fully usable **silent and headless** (no audio extras
installed).

### Principles (extend SPEC §1; new ones numbered from 8)

8. **Pages generalize screen.** vocodeck's `screen` verb becomes sugar
   for one pinned markdown page. One content model for everything an
   agent shows a human; every page type is annotatable.
9. **One wake channel.** A human annotation, question, or typed input
   wakes a parked agent exactly like a transcript — through `listen`.
   No second poll loop, no separate review daemon. Delivery is
   **at-least-once with idempotent item ids; the ledger is the truth**
   (§4.2) — never claimed exactly-once.
10. **Buildless client, packaged honestly.** Vanilla ES modules — no
    bundler, no frontend build artifact, ever. Typed via JSDoc +
    `tsc --noEmit --checkJs` (a pinned dev-only Node tool in CI, not a
    ship dependency). Third-party JS **vendored and pinned** (no CDN —
    the released package *serves* airgapped; features that shell out to
    the network, like `gh`, are capability cells that degrade, §3.2).
11. **Review without voice.** The base install (`uv sync`, zero extras)
    runs the daemon + workbench as a silent review cockpit. Audio
    (`stt`/`tts`/`ptt`) and `pty` are extras; capability degrades
    per-cell, never breaks (SPEC principle 4 applied to the package).
12. **Deterministic-first review material** (carried from diff-annotate):
    diffs, contracts, evidence come from git/tool facts; LLMs may
    summarize and annotate but are never the substrate of truth.
13. **The in-page chat explains, never verdicts** (lane independence,
    carried): review judgment belongs to the human; the agent answering
    in-page questions speaks in the librarian register.
14. **The browser is not trusted by proximity.** Loopback-only binding
    protects against the network, **not** against hostile web pages in
    the user's own browser (CSRF, cross-site WebSocket hijacking). Every
    mutating surface requires the workbench token + Origin discipline
    (§8) — this amends a latent gap in the foundation (§8.5).

---

## 2. Vocabulary (extends SPEC §3)

- **Workspace** — one checkout directory, keyed by
  **`realpath(repo_root)` alone** (grill decision 20): `realpath`
  already distinguishes git worktrees; the current branch is **display
  state** ("now @ branch"), never identity, so a mid-session
  `git switch` cannot orphan pages or findings (diff pages are keyed by
  their `ref`, so per-branch diffs coexist in one workspace). Detached
  HEAD / rebase / branch rename are non-events. Workspaces are
  **derived, never user-created** — herdr-style drag/group arrangement
  is a later, client-side rail preference that arranges workspaces but
  never defines them (§10 later column). Owns pages, findings, asks;
  persisted in a manifest; fully usable with **no agent attached** (the
  pseudo-session property).
- **Sessionspace** — the fallback home for a session registered with
  **no repo** (register's `repo?`/`branch?` are optional): keyed by
  `(host, realpath(cwd))`, holds only agent-scoped pages
  (screen/terminal), no review features. Rendered in the rail under
  "no repo". Not persisted.
- **Repo group** — workspaces sharing `git rev-parse --git-common-dir`;
  a rail grouping level, not an entity.
- **Page** — one typed content unit in a workspace:
  `{page_id, type, title, rev, pinned, scope, ref?}`.
  `scope ∈ workspace | agent` — a diff belongs to the checkout (two
  agents in one worktree share one review surface); a screen or terminal
  belongs to one agent. Agent-scoped pages are annotatable too; their
  findings wake **that agent**, not the workspace primary (§4.3).
- **Rev / stale** — re-pushing a page with the same `(type, ref)` bumps
  `rev`; findings on older revs get a stale chip and are **kept — never
  dropped, never silently re-anchored** (diff-annotate Slice 2b
  semantics, ported whole).
- **Finding** — a human annotation:
  `{finding_id, page_id, rev, anchor, kind: concern|question|nit,
  blocking: bool, text,
  status: open|addressed|disputed|wont-fix|withdrawn,
  note?, commit?, answer?}`. `withdrawn` is set by the human (the
  browser's "remove"); withdrawn question-findings also close their
  linked ask. "Pending" ≡ `status: open`. Diff anchors are
  `{file, side: new|old, startLine, endLine}` — byte-compatible with
  diff-annotate's output contract so existing consumers (onebrain lanes)
  keep working.
- **Ask** — a question from the workbench chat, routed to an attached
  agent; answered in markdown.
- **Agent state (the dot)** — a display state derived per session, see
  §6. Never asked of the model; derived (SPEC principle 3).

---

## 3. The pages model

### 3.1 Page types

| type | scope | v1 | content |
|---|---|---|---|
| `screen` | agent | yes (pinned, unclosable) | markdown; `mode: show|append`; exactly SPEC §8.1's screen verb |
| `terminal` | agent | yes (pinned for managed sessions) | live pane mirror (tmux) or byte stream (pty), §5 |
| `diff` | workspace | yes (pinned while present) | resolved unified diff, annotatable per line/range |
| `doc` | workspace | yes | markdown by path (confined) or inline content (virtual) |
| `html` | workspace | later | iframe artifact — security shape reserved NOW: sandboxed iframe, **no workbench token reachable from the frame**, no parent-DOM access, size limits; exact sandbox flags decided when built |
| `decision` / `testev` | workspace | later | ported from diff-annotate as demand returns |

Pinned pages are unclosable in the UI; pushed `doc` pages are closable
(closing hides, never deletes — the manifest keeps them).

### 3.2 Push & compatibility

- `POST /v1/bridge/screen` is **unchanged on the wire** (same payload,
  same behavior, same `screen.updated` event and `voco screen <name>`
  dump) and becomes an upsert of that session's pinned screen page.
  `voice_screen` untouched.
- New bridge verb — `POST /v1/bridge/page`
  `{session_id, type, name?, content? | path? | source?, mode?}`:
  - `doc`: `path` (realpath-confined, §8) or `name+content` (virtual).
  - `diff`: `source: {pr: N} | {branch: base?} | {staged: true} |
    {diff_file: path}`, or raw `content` (a patch; never live-tracked).
  - **Identity is re-asserted on every workspace verb** (page push,
    findings, finding_status, ask_reply, say, screen, listen): the
    adapter sends its current derived identity (`identity` in POST
    bodies; a JSON `identity` query param on GETs) and the daemon
    re-keys the session to it before resolving — no verb ever routes
    on stale register-time facts. (2026-07-06 dogfood: a stale root
    500'd git sources and "no such doc"'d files that existed.)
  - **Errors carry context**: every bridge 4xx names the resolved
    workspace root and the attempted path/ref/id, and the bridge never
    returns a bare 500 — a middleware wraps unexpected exceptions in a
    JSON body naming the exception. Session caches on the adapter side
    key on the FULL cwd, never its basename (two checkouts sharing a
    basename must never share a session).
- **Where resolution runs — the local/remote split** (fixes the remote
  filesystem reality; SPEC §9.1 remote sessions are text-only tunnels
  and the daemon can never read their disks):
  - Sessions carry a derived capability cell **`local_fs`**: true iff
    the session's `host` is the daemon's host.
  - `local_fs: true` → `path`/`source` resolve **daemon-side**, `git`/
    `gh` subprocesses running **with the workspace root as cwd**
    (invariant: never the daemon's cwd — this is what makes worktrees
    correct).
  - `local_fs: false` → the daemon rejects `path`/`source` softly with
    a one-line hint; the **adapter** resolves instead (voco-cli/voco-mcp
    on the remote box runs the same git commands there and pushes
    `content`). Same UX, different resolver; the page records which.
  - Pinned resolution commands (deterministic-first): `pr` →
    `gh pr diff N` (**requires `gh` + network + auth — a capability
    cell**; absent → soft error naming the missing piece, never a
    crash); `branch` → `git diff $(git merge-base HEAD <base||default
    branch>)..HEAD`; `staged` → `git diff --cached`; `diff_file` →
    read, confined like docs.
- Same `(type, ref)` → replace + `rev++` + `page.updated` event; new
  ref → append. A diff re-push also records the **inter-diff** vs the
  replaced rev **when W5 lands** (until then: rev bump + stale chips
  only — this sentence is the single source of truth on that split).
- Humans can also add a diff from the workbench (PR number / branch /
  staged picker) — daemon-side resolution against the workspace root;
  works with no agent attached.

### 3.3 Output contract (export)

- Trigger: browser **Export** button, or
  `voco review export [--workspace <key|name>] [--out <path>]` — with
  no `--workspace`, the workspace resolves from the **cwd**
  (derive-don't-ask; onebrain wiring is just "run it from the repo").
- Default out: `<workspace-data-dir>/review-<UTCstamp>.json` (printed);
  `--out` overwrites atomically. The default never writes into the
  checkout (grill decision 25).
- Contents: the legacy-compatible JSON array
  `[{file, side, startLine, endLine, concern}]` — **diff-page findings
  only, excluding `withdrawn`** (matching diff-annotate's legacy
  contract) — plus the full anchors sidecar `<out-basename>.anchors.json`
  (all pages, finding ids/kinds/statuses/answers, rev + stale flags).
- Export is keep-alive (a flush, not an exit); findings publish live
  long before export.

---

## 4. Findings, asks, and the unified wake

### 4.1 Ledger

Per-workspace findings ledger, persisted in the manifest. Authorization:
a session may read and mutate **only its own workspace's ledger**
(workspace resolved from refreshed identity); within a workspace the
ledger is shared truth for all attached agents. Mutations:

- the browser: add / edit / withdraw (status → `withdrawn`) / set
  kind/blocking — publishes live on add;
- the agent: `GET /v1/bridge/findings?session_id=…[&pending=1]`
  (pending ≡ `open`), and `POST /v1/bridge/finding_status
  {session_id, finding_id, status, note?, commit?}` — agents may set
  **status/note/commit only** (never text/kind/anchor; `answer` lands
  via `ask_reply`). Chips update live in the open page.
- Conflict model (multiple tabs, agent + human racing): **last-writer-
  wins**; every mutation emits the finding's **full state** in the
  event, so stale views converge; commands are idempotent by
  `finding_id`. No CAS in v1 — the write set is one human + a couple of
  trusted agents.

### 4.2 Wake through listen (the design center)

Findings/asks reach agents through the existing park — but **only for
sessions that opted in**: register's `capabilities` gains `review`
(SPEC principle 4 — the matrix, not a new mechanism). Sessions without
it never see review payloads; the workbench shows their workspace's
pending count anyway (the ledger doesn't care who's listening).

For `review`-capable sessions, `listen` returns gain, additively:

- pending review items **always ride along** in `queued` (as
  `{kind: finding|ask, id, …}` entries alongside transcript entries) —
  so review can never be starved by voice traffic;
- a parked listen with nothing else pending wakes as
  `{status: "review", items: [...]}` — the wake reason, not the only
  delivery path.

Delivery semantics (honest, not exactly-once): **at-least-once,
idempotent by item id; the ledger is authoritative.** An item is
delivered on wake and re-attached to `queued` until its status leaves
`open` (finding) or it is answered (ask) — an agent that crashes
between wake and action simply sees it again; a duplicate
`finding_status`/`ask_reply` is a no-op. Review items mint **no
`turn_id`** (turns are utterance-anchored); agent `say`s about them
follow the existing no-turn attribution rules, and digest entries
reference item ids.

Consequences:

- **No review poll loop.** diff-annotate's `poll`/`reply` subcommands
  and its planned firstmate check-shim are superseded by the park the
  agent already holds.
- `voice_listen` (MCP) and the `listen.sh` scripts declare the `review`
  capability and print review items as they print transcripts; the
  agent-side discipline text (SPEC §8.4) grows one line: *"Treat review
  findings and questions as user input: address them, then report via
  finding_status / ask_reply."* Third-party adapters that never opt in
  are untouched — that is the additive-evolution story for a bridge
  return enum.
- A `question`-kind finding is also an ask (one ledger entry, linked) —
  the reply lands under the finding card; withdrawing the finding
  closes the ask.

### 4.3 Asks (the in-page chat) & routing

- Browser: `ask {text, context?}` (context = selected file/lines/quote).
- Routing — the **primary agent** of the workspace, elected in order:
  (1) the daemon's active session, if it lives in this workspace;
  (2) the workspace's only attached session; (3) the most recently
  parked `review`-capable session; UI selector overrides per-workspace.
  Workspace-scoped findings wake the **primary only** (others read the
  shared ledger; no duplicate work by design). Agent-scoped page
  findings wake **that page's agent**.
- Agent answers via `POST /v1/bridge/ask_reply {session_id, ask_id,
  markdown}`; the browser renders it with the shared markdown renderer.
- No `review`-capable agent attached → the ask queues and the UI says
  so honestly ("no agent attached — will deliver when one registers"),
  mirroring the no-active-session earcon rule (SPEC §5.4 rule 6).
- The librarian register (principle 13) is enforced by instruction
  text, not server code — same as diff-annotate.

---

## 5. TerminalBackend (adapter seam)

Managed-session terminal handling becomes a role-named port with **two
concurrent implementations — both available at once, chosen per spawn**:

```
TerminalBackend (protocol)
  spawn(cmd, cwd, env, host?) -> handle
  kill(handle)                      alive(handle) -> bool
  send_text(handle, text)           send_key(handle, key)   # Escape etc.
  capture(handle) -> str            # tmux: capture-pane; pty: ring buffer
  stream(handle) -> AsyncIterator[bytes] | None   # pty only; tmux -> None
  resize(handle, cols, rows)        # pty only; tmux no-op
  list(host?) -> [handle]
```

| | `tmux` (exists: `adapters/tmux.py`) | `pty` (new) |
|---|---|---|
| platforms | Unix, remote via ssh | Unix (`pty` stdlib), **Windows (ConPTY via `pywinpty`)** — extra `pty` |
| survives daemon restart | **yes** (its superpower — keep it) | **no in v1** — stated honestly; a detached holder process is v2 |
| native terminal attach | `tmux attach` | none (workbench or `voco attach-cmd` only) |
| workbench terminal page | **read-only mirror** (poll capture, as today's peek). The page's input box is honestly labeled "send as user input" — it dispatches through `say_as_user` (bridge semantics), it does **not** type into the pane; real typing = `tmux attach`, or v2 `inject` | **live xterm.js** — standard editor-terminal semantics: **interactive on focus** (click to focus, focused pane receives keys, visible focus indicator; no per-session unlock step — grill decision 23) |
| inject (v2 SPEC §8.4) | send-keys | PTY write |

- Selection: `voco new claude --backend tmux|pty`, `session.spawn`
  payload gains `backend`; config sets the default per platform
  (`tmux` where present, `pty` on native Windows). **W4 amends SPEC
  §9.2**: native Windows gains managed sessions via ConPTY (the
  foundation's attach-only claim described M0–M3).
- Per-session **terminal capability cells** ride the registry and
  snapshot (`{stream, capture, send_keys, resize, survives_restart,
  native_attach}`), so the UI degrades per-cell (no pty extra / no
  tmux → the terminal page hides or downgrades; xterm.js being
  vendored in base is static weight only, never a requirement).
- The watcher (`watcher.py`) is backend-agnostic — `pane_state.classify`
  is pure text and consumes `capture()` from either.
- **PTY stream mechanics** (W4 build guidance): dedicated WS route
  `/v1/term/{session_id}` — binary frames of raw PTY bytes; JSON text
  frames for control (`{resize: {cols, rows}}`). On open, the server
  replays the scrollback ring buffer (default 256 KiB, config) as the
  first frames. Backpressure: if the browser's socket send-buffer
  stalls past a bound, drop-oldest from that client's queue (the ring
  buffer, not the client, is the recovery source — reconnect replays).
  Daemon restart / handle death closes the socket (code + `term.closed`
  event). Keyboard input frames are written to the PTY verbatim;
  bracketed paste is the terminal app's business, not ours.
- **Security**: `/v1/term/*` is a mutating surface — workbench token +
  Origin discipline (§8.5) mandatory, plus the bearer token when
  configured. It is never reachable with neither.
- Externally-started sessions remain first-class attach-only (decision
  006 unchanged); their terminal page simply doesn't exist.

---

## 6. Agent state abstraction (harness-agnostic by construction)

Already mostly present; this section names the contract so Claude,
Codex, and future harnesses need **no new code paths**:

- **Bridge facts** (harness-agnostic by construction): session states
  `parked | working | idle` (+ `stale` display flag) per SPEC §8.2.
- **Pane hints** (output heuristics): `waiting | working | shell`
  from `core/pane_state.py` — patterns are *data*; new harnesses extend
  the pattern lists, never add branches.
- **Derived display state** — one pure function in `core/`
  (`agent_state.py`), the only thing the UI ever renders. **Total
  precedence** (every session maps to exactly one dot):

  `gone` > `blocked` > `working` > `listening` > `stale` > `idle`

  | dot | derivation |
  |---|---|
  | `gone` | hint `shell` (harness exited) or managed handle dead |
  | `blocked` | hint `waiting` (confirmed, two sightings) **and bridge not `parked`** — a parked agent is by definition not blocked; bridge truth wins |
  | `working` | bridge `working`, or hint `working` |
  | `listening` | bridge `parked` |
  | `stale` | bridge `idle` for > `stale_after` (dimmed) |
  | `idle` | bridge `idle`, no hint |

  Known risk (owned): a harness that prints a prompt-shaped pattern
  while processing can still mislabel `blocked`; the two-sighting rule
  and the parked-override bound it, and patterns are per-harness data —
  tune there, never in code. Carried on `session.state` events
  additively (`payload.display_state`), so the debug UI and workbench
  render the same truth.
- Harness-specific surface area is exactly three places, all existing:
  identity derivation at register, pane-pattern data, spawn command
  templates. A future harness self-reporting semantic state
  (`voice_status`, v2 parking lot) maps into the same enum — it never
  bypasses it.

---

## 7. Workbench UI (the ported client)

- **Source**: diff-annotate's `lib/client/` lifted — panel contract
  `{id, slot, mount(container, ctx), onEvent(evt)}` with
  `slot ∈ rail | editor | dock | status`, the store with
  `subscribe(kind)`, editor-tabs, shared markdown renderer, diff table
  with exact `data-file`/`data-side`/`data-line` row attributes.
  Adaptations: `bus-client.mjs` speaks the SPEC §10 WS envelope
  (snapshot-first, ignore-unknown) instead of SSE; rail rebuilt per the
  layout below; styles retained.
- **Layout**:
  - **Rail (left)** — top: **repos → worktree workspaces → attached
    agents** with state dots (§6), plus a "no repo" group for
    sessionspaces; bottom: **flat agent roster** across workspaces
    (presence + quick-switch; click → jump to its workspace, focus its
    terminal page).
  - **Editor (center)** — the selected workspace's tab strip: pinned
    first (`Diff`, then per-agent `screen·<callname>`,
    `term·<callname>`), pushed docs after, closable.
  - **Dock (right)** — chat (asks) + findings list with status chips.
  - **Status bar** — mic/duplex/attention state, turn state, active
    session, live `stt.partial` ticker. Voice controls live here, not in
    a floating widget.
- **Serving**: workbench at `/` from `src/voco/server/static/`
  (client `.mjs` + `vendor/`); the current `ui.html` moves to `/debug`
  and is kept — it is the minimal protocol reference client
  (PROTOCOL.md's promise) and the workbench's fallback. W0 updates the
  README/PROTOCOL lines that point at `/`.
- **No framework** (decision): the panel contract is the component
  system. Types via JSDoc + `tsc --noEmit --checkJs` in CI — a pinned
  dev-only tool (no lockfile-less `npx`; pin the TypeScript version),
  **no frontend build artifact ever** (that is what "buildless" means;
  Node-as-dev-linter is fine). If a future panel genuinely outgrows
  this, Preact via buildless `htm` may be introduced **per panel** —
  never an app-wide conversion.
- **Vendored JS** (pinned versions + licenses in one `vendor/MANIFEST`
  file; no CDN): `marked`, `highlight.js`, `mermaid`, `xterm.js`
  (+ fit addon), **DOMPurify**; CodeMirror 6 only when the file-viewer
  page ships (later). CVE bumps: checked in CI, advisory — an
  **accepted risk**, named here.
- **Rendering safety** (new; the workbench renders agent-supplied
  markdown, ask replies, and doc files): all markdown-derived HTML
  passes through DOMPurify before insertion; diff/finding/terminal text
  is only ever set via `textContent`/xterm APIs; the daemon serves the
  workbench with a CSP (`default-src 'self'; connect-src 'self' ws:`,
  no inline script) so a sanitizer miss doesn't become daemon control.
- `say_as_user` (SPEC §10) is the workbench's typed-input box — already
  specified, now used.

---

## 8. Workspace persistence & security (ported hardening)

- **Manifest**: `~/.local/share/voco/workspaces/<key>/manifest.json`
  (the same per-workspace dir exports land in; `VOCO_DATA_DIR` override
  for tests) — pages (path-backed by path,
  virtual by content, diff by text + recorded source), findings, asks,
  revs. In-server autosave (changed-only, flushed on shutdown); launch
  reconciles pushes against it (unchanged source keeps its rev — no
  spurious staleness); `--fresh` opts out. This supersedes SPEC §8.3's
  "digests in-memory v1" only for workspace data; digests/queues stay
  as specified.
- **Sensitivity** (named): manifests contain proprietary diffs, docs,
  and review text. Data dir `0700`, files `0600`; documented in README;
  `voco review forget <workspace>` purges a manifest (later slice, but
  the promise is made now).
- **Single-writer**: one daemon per manifest — sidecar lock created
  atomically (`O_CREAT|O_EXCL`) carrying `pid` + **process start-time
  nonce** (pid-reuse defense); a dead holder (nonce mismatch or gone
  pid) is taken over; non-Linux degrades to pid-only (diff-annotate
  precedent). Exotic filesystems (NFS, containers) are out of scope —
  named, not solved.
- **Confinement**: any path served to the browser (docs, later files)
  is realpath-resolved and must land inside the workspace root or an
  explicitly pushed parent dir — **re-checked on every read**, not once
  at push (that is the TOCTOU mitigation: a swapped symlink fails the
  next read's check; diff-annotate's model). Outside → 400/404. Docs
  are served **by id, never by path**.

### 8.5 Browser-facing auth (amends the foundation)

Loopback binding is necessary, not sufficient: hostile web pages in the
user's browser can send cross-origin POSTs and open cross-site
WebSocket connections to `127.0.0.1:7777` (WS has no CORS preflight).
The foundation already exposes `say_as_user` and `session.spawn` over
the WS command channel with no Origin check — a latent gap this spec
fixes daemon-wide, because the workbench raises the stakes (finding
mutations, page pushes, and at W4 an interactive terminal):

1. **Origin discipline (always on, no kill switch):** WS upgrades
   (`/v1/events`, `/v1/term/*`) and every mutating HTTP route reject
   requests bearing a non-loopback Origin. **Any loopback-host Origin
   passes regardless of port** (`127.0.0.1:*`, `localhost:*`,
   `[::1]:*`) — hostile pages live on foreign hosts, and this keeps SSH
   tunnels and re-numbered local port-forwards frictionless (grill
   decision 22). Proxy fronting (Coder-style HTTPS origins) is
   config: `[server] allowed_origins`. Requests with **no** Origin
   header (curl, adapters, CLI) pass — this defends against browsers,
   which always send it.
2. **Workbench token:** minted per daemon run, injected into the served
   page (never readable cross-origin), required (`x-voco-wb` header /
   WS subprotocol) on: WS **command** execution (events remain
   readable), all browser mutations (findings, asks, page ops, export),
   and `/v1/term/*` absolutely. CLI/adapters authenticate with the
   bearer token (when configured) or pass tokenless from
   no-Origin + loopback as today.
3. **Bearer token** (SPEC §9.1) unchanged for remote/shared-host use;
   when configured it gates **all** mutating surfaces — bridge, control
   POSTs, WS commands, term — not the v0.1 partial list.
4. Reads needed to boot stay open: `/`, static assets, `/debug`,
   `/v1/events` (snapshot + events are observability, per foundation).

The foundation-side fix (Origin check on today's WS commands) should
land as its own small patch regardless of workbench timing.

---

## 9. Protocol additions (SPEC §10 extension — all additive)

**Snapshot** (extended — reconnect must rebuild the workbench; there is
no replay): gains `workspaces` (key, repo, branch, page metadata —
id/type/title/rev/pinned/scope, **not** content), per-workspace finding
counts by status, ask counts, per-session `display_state` and terminal
capability cells. Page *content* is fetched on demand (routes/commands),
never carried in the snapshot.

Events: `workspace.updated {key, repo, branch, pages: n}`,
`page.updated {workspace, page_id, type, rev, action: added|updated}`
(`screen.updated` is **kept**, exact legacy payload, emitted alongside
for screen pages — existing consumers unaffected),
`finding.added|updated` (payload = the finding's **full state** — the
last-writer-wins convergence mechanism, §4.1), `ask.created|answered
{ask_id}`, `term.opened|closed {session_id}`.

Commands: `workspace.list`, `page.close|reopen`, `finding.add|update|
withdraw` (browser-side mutations ride commands, not bare REST, so the
debug UI and tests reach them too; idempotent by id), `ask.create`,
`review.export`.

Bridge verbs (§8.1 extension): `page` (push), `findings` (read),
`finding_status`, `ask_reply`; `listen` gains the `review` status —
**gated on the session's `review` capability** (§4.2).

MCP tools added to voco-mcp: `page_push`, `review_findings`,
`review_reply` (answers an ask **or** sets a finding status — one tool,
fewer prompts). `voice_*` tools unchanged; voco-mcp registers with the
`review` capability.

`scripts/gen_protocol.py` picks all of this up; PROTOCOL.md stays the
client-facing reference.

---

## 10. Scope: ported core vs. later vs. never

| Core (this spec) | Later (demand-driven, page types make them additive) | Never (carried rejections) |
|---|---|---|
| workspaces + manifest + worktree keying | `html` page (lavish-2.0 artifact review; security shape reserved in §3.1) | publish findings to PR comments (owner decision 2026-07: captain-private) |
| pages + screen compat + doc/diff push | test-evidence + decision pages | agent-activity viewer |
| diff annotation + findings ledger + status round-trip | live-git tracking + **inter-diff** (both W5) | generic graph canvas |
| asks/chat through the wake channel | file explorer + CodeMirror viewer + palette/codegraph macros | hub proxy |
| `review` capability + listen status + MCP tools | contract-impact ("who uses this?") | |
| TerminalBackend (tmux + pty) + xterm page | keyboard-first review flow (j/k, mark-reviewed) | |
| rail/dock/status workbench shell + §8.5 auth | streaming ask answers; `voco review forget` | |
| | herdr-style rail grouping (client-side arrangement of workspaces; never data identity) | |

The later column is not a promise; each item returns only as a page
type or panel behind the existing seams.

---

## 11. Build slices (each shippable; tests ported per slice)

- **W0 — pages + shell + auth**: minimal workspace identity (key +
  in-memory model + sessionspaces — persistence is W1), pages model in
  core (screen verb → pinned page), protocol additions incl. snapshot
  extension, **§8.5 Origin + workbench token** (land the auth before
  the surfaces), workbench shell at `/` (rail with workspaces/agents/
  state dots, tabs, dock, status bar) rendering screen + doc pages
  live. Debug UI moves to `/debug`; README/PROTOCOL pointers updated.
  *Exit: two agents in two worktrees each show screen pages in one
  workbench; a cross-origin mutation attempt is rejected.*
- **W1 — diff review**: diff page (local/remote resolution split,
  workspace-cwd invariant, pinned commands), annotation UX, findings
  ledger + live publish + status round-trip, manifest persistence +
  single-writer lock + file modes, export (`voco review export`,
  legacy-compatible JSON + sidecar). *Exit: a real PR reviewed
  end-to-end in the workbench; output JSON byte-compatible with
  diff-annotate's.*
- **W2 — the wake**: `review` capability + listen status + queued
  ride-along + at-least-once redelivery, `page_push` /
  `review_findings` / `review_reply` MCP tools, ask/chat panel +
  primary election, discipline-text update. *Exit: annotate a line →
  parked Claude Code wakes, fixes, sets `addressed` → chip flips live;
  kill the agent mid-wake → item redelivers to the next park.*
- **W3 — worktrees first-class**: repo grouping in rail (`common_dir`
  in identity), `voco new --worktree <branch> [--from <base>]`, clean
  worktree removal on kill (never dirty ones). *Exit: spawn three
  agents in three new worktrees from the rail, dots live.*
- **W4 — TerminalBackend**: the port + pty implementation (Unix pty,
  Windows ConPTY), `/v1/term/*` WS stream (replay/backpressure per §5),
  terminal capability cells, xterm.js terminal page, per-spawn
  `--backend`, Windows profile validated. *Exit: managed session on
  native Windows with a live interactive terminal tab; a tokenless
  term connection is refused.*
- **W5 — rev/staleness depth**: inter-diff on re-push, since-rev
  banner, stale chips with `area_changed`, live-git tracker
  (interval re-resolve, `--no-live` per workspace). *Exit:
  diff-annotate's re-review flow reproduced.*

---

## 12. Risks & honest failure modes

| Risk | Exposure | Mitigation |
|---|---|---|
| Rewrite regresses diff-annotate's subtle invariants | staleness/anchor/confinement bugs | tests ported per slice as the oracle; DESIGN.md follow-ups treated as a checklist |
| Hostile web page hits localhost daemon | input injection, spawn, terminal (W4) | §8.5: always-on Origin discipline + workbench token; foundation patch for today's WS commands |
| XSS via agent-supplied markdown | daemon control from a rendered doc | DOMPurify on all markdown HTML + CSP + textContent discipline (§7) |
| Two products in one repo | audio deps burden review-only users | §1.11 extras split; workbench runs with zero extras; CI runs the base install headless |
| PTY sessions die with the daemon | lost managed sessions on restart | stated in docs + spawn output; tmux backend exists for exactly this; holder-process split is v2 |
| Review wake mis-routed (multi-agent workspace) | duplicate or missed work | primary election (§4.3) + agent-scoped wake rule + shared ledger as truth |
| Agent crashes between wake and action | lost review item | at-least-once redelivery until status change (§4.2); ledger authoritative |
| Remote session pushes a path | daemon can't read it | `local_fs` cell: soft reject + adapter-side resolution (§3.2) |
| Vendored JS drift/CVEs | stale libs in releases | pinned versions + licenses in vendor/MANIFEST; CI bump check — advisory, an accepted risk |
| Scope creep from diff-annotate's accretion | port never ships | §10 table is law; later-column items need a new decision, not momentum |
| JSDoc typing erodes | untyped client growth | `tsc --noEmit --checkJs` (pinned) is CI-blocking from W0 |
| Harness prints prompt-shaped output while busy | wrong `blocked` dot | two-sighting rule + parked-override (§6); patterns are per-harness data |

---

## 13. Decision log

2026-07-06, from the merge deliberation (v0.1):

1. **One package**: port diff-annotate into vocodeck2; the Node service
   is not shipped (it remains the author's fallback during the port).
   Reason: GitHub release quality — one runtime, one daemon, one URL.
2. **Client lifts, server rewrites**: the ~4.6k-line buildless client is
   adapted, not rewritten; the ~4.4k-line Node server is rewritten in
   Python against its ported tests.
3. **No UI framework**: vanilla ES modules + panel contract; JSDoc +
   `tsc --checkJs` for types; vendored pinned JS, no CDN. Escape hatch:
   buildless `htm`+Preact per panel, never app-wide.
4. **screen → pages**: `screen` becomes the pinned markdown page; wire
   compat kept (`/v1/bridge/screen`, `voice_screen`, `screen.updated`).
5. **One wake channel**: findings/asks wake `listen`; no review poll
   loop; supersedes diff-annotate's poll/reply and its planned
   firstmate shim.
6. **TerminalBackend port with both backends live at once**, selected
   per spawn (`--backend tmux|pty`), platform-default config. pty via
   ConPTY unlocks managed sessions on native Windows.
7. **herdr: concepts only** (AGPL) — real PTYs, state dots,
   detach/reattach; no code, no dependency.
8. **Worktree-aware workspaces**; `voco new --worktree` first-class;
   clean-only auto-removal. (Key shape revised by review — see 13 and
   the §2 open fork.)
9. **Agent state**: harness-agnostic by construction — bridge facts +
   pane-hint data + one derivation function (§6); harness-specific code
   confined to identity, patterns, spawn templates.
10. **Rail layout**: repos → worktrees → agents on top; flat agent
    roster below; dock right; voice state in the status bar.
11. **Output contracts frozen**: findings JSON + anchors sidecar stay
    byte-compatible with diff-annotate for downstream consumers.
12. Work proceeds on branch `workbench`.

2026-07-06, from the adversarial review (v0.2 — Codex, 40 findings,
~36 applied):

13. **Workspace key is an open fork** (Codex finding 6): branch-in-key
    breaks on `git switch`; recommendation is `realpath(root)` alone
    with branch as display state. Decided at the grill.
14. **§8.5 browser auth is mandatory and daemon-wide** (findings 1/2/9):
    always-on Origin discipline + per-run workbench token; bearer gates
    everything when configured; foundation gets the Origin patch
    independently.
15. **Remote sessions resolve adapter-side** (finding 3): `local_fs`
    capability cell; daemon-side git/gh only for same-host sessions.
16. **`review` is a registered capability** (finding 4): the listen
    `review` status is opt-in; third-party adapters unaffected.
17. **Delivery is at-least-once, ledger authoritative** (finding 5):
    idempotent ids, redelivery until status change, no turn minting.
18. **Sessionspaces** for repo-less sessions (own finding): agent
    pages need a home when there is no workspace key.
19. Rejected: Codex finding 32 (§1.13 exists — principles are numbered
    8–14 in §1; reworded to "principle 13" to remove the ambiguity).

2026-07-06, from the Phase 0 grill (v1.0 — 7 decisions):

20. **Workspace key = `realpath(root)` alone.** Branch is display
    state; per-branch diffs coexist via page `ref`. Herdr semantics
    investigated first: its workspaces are user-arranged containers
    with no git binding — adopted as a later client-side rail
    *arrangement* feature, never as data identity (derive-don't-ask
    governs the data model).
21. **Wake routing: primary election as drafted** (§4.3 three-step
    rule + per-workspace UI override) over broadcast-and-race and
    explicit-assignment-only.
22. **Origin policy: any loopback-host origin, any port**, plus
    `[server] allowed_origins` for proxy fronting; no kill switch.
23. **Terminal input: interactive on focus** — standard editor-terminal
    semantics with a visible focus indicator; no per-session unlock.
24. **Data dir confirmed from codebase conventions**: the repo is
    XDG-shaped (`~/.config/voco`, `~/.cache/voco`, `~/.local/state/
    voco`), so durable review data lands in `~/.local/share/voco/
    workspaces/` (`VOCO_DATA_DIR` override).
25. **Export: data-dir default + `--out`, cwd-resolved workspace**;
    checkouts are never written to by default.
26. **Slice order stands: W0→W5 review-first**; pty/Windows terminal
    stays W4 (WSL2 tmux is the managed-session stopgap until then).

2026-07-06, after the first real dogfood (post-W5):

27. **Staleness kill: identity rides every workspace verb** (§3.2) —
    the register-time identity snapshot is a cache, not a truth; the
    adapter's current cwd/worktree wins on every page/findings/listen
    call. Chosen over "re-register on mismatch client-side" because
    the client cannot see the server's stale copy (state restore), and
    over TTL-based expiry because staleness is not time-shaped. Plus:
    contextual 4xx bodies (root + attempted path), a no-bare-500
    middleware, and full-cwd session cache keys client-side.
