# BUILD-CONSOLE — the mk3 UI rebuild (plan + running journal)

**Pinned design: `design/index7.html` (FL push, blessed + ported
2026-07-10).** Supersedes index5 for everything it covers; index5 rules
still govern where index7 is silent. Style tokens are immutable without
captain sign-off. Design lineage for archaeology: index.html (round 1)
→ index2 (D·CONSOLE) → index3 (rejected restyle) → index4 (mk2 quiet)
→ index5 (mk3 final) → index6 (mk4 THE DECK) → **index7 (FL push)**.

**index7 port (SHIPPED 2026-07-10), the deltas over mk4:** one SVG icon
sheet (`icons.mjs`, `ic()` is the only SVG builder — createElement
cannot make SVG); the command bar is ADE chrome only ([voco ● host] ·
⚙); the DECK HEADER is the master strip — the one input (mount-once:
value/focus survive re-renders), ROUTE display (status only, by design),
● talk / ■ stop, the 4-pos attention switch (direct-select; WAKE
disables itself on mic.state `wake_available:false` with an actionable
toast), mic lock (fixed width — no reflow), duplex click-toggle,
working/ready LCD, minimize toggle; there is NO master card — every
card is an agent channel (plate = state-colored module icon [the icon
IS the LED, word adjacent] + name + state·age + ⊞ overview→focusAgent);
3-line say-tail (agent's voice ONLY — user text lives in the
transcript); queue as 4 step cells + amber count with the path-never-
clips flex contract; deck is height-resizable (grip above the header,
`--deckh`, persisted); ledger tabs show counts as step lights; status
line speaks LCD (LED+host well, MIC→NAME well, live VU). Meters/VU are
SIGNAL-driven: the daemon now emits throttled `mic.level` (~10Hz,
post-AEC, one trailing zero at silence) and the deck updates meter/VU
slots out-of-band (a dedicated "miclevel" store notify — never a panel
re-render at 10Hz); without level events (older daemon) meters fall
back to the state pulse. A11y: aria-pressed on talk/lock, radiogroup
switch, tablist ledger, focus-visible outlines.

**Design anchor (captain, 2026-07-09): "reminds me of FruityLoops — I
like that."** The DAW/instrument reading is the intended one and future
UI work should lean INTO it, not sand it off: THE DECK as a channel
rack of agent cards, fixed MIC patch points like a patch bay, the
presence strip as a transport bar, dark hardware-panel surfaces with
per-channel state lights. When a UI decision has a "software dashboard"
option and a "music-production hardware" option, pick the hardware one.

## Tokens (the contract — from index5)

- Surfaces: b0 `#131417` (work) · b1 `#17181c` (chrome) · b2 `#1c1e23`
  (raised) · b3 `#24262c` (active) · hover `#20232a`
- Lines: `#26282e` seams · `#33363e` strong (frame, controls)
- Ink: t0 `#d6d9de` · t1 `#9aa0a9` · t2 `#5f646d`
- Steel `#6ea3d8` = selection + PR/issue chips. Amber `#d9a334` = mic ·
  blocking · queue · dirty · needs-you. Green `#43b581` / red `#d95c4f`
  = live signal, diff/ahead-behind. Selection wash `#3d63804d`.
- Type: 12px mono (data/labels) · 13.5px sans (prose) · 10px caps
  micro-labels (letterspaced) · 10.5px mono meta.
- Hairline budget: zone seams, command-bar cells, canvas bars, table
  head, editor outline, control edges. Interior separation = surfaces.
- Square LEDs (6px). No border-radius. No shadows. No animation except
  meters (and reduced-motion stills those).

## Layout (grid, replaces strip/rail/work/dock/status)

```
rows: 36px command bar / 1fr mid / 210px console / 24px status
mid cols: 266px fleet tree / 1fr canvas / 220px channel rack
```

## Full inventory (the don't-miss-anything list)

### 1. Command bar
- [ ] `voco ● 127.0.0.1:7777` cell (LED = daemon state; red + "retry in
      Ns" when disconnected)
- [ ] Prompt = THE one input (real input, `>` glyph, idle placeholder
      `say "deck …" or type`, routes to mic holder)
- [ ] `route → <HOLDER>` live indicator (amber name)
- [ ] Keys cell: `F13 ptt` · `⌘K cmd` · `⚙` (opens settings modal)

### 2. Fleet tree (left, 266px)
- [ ] `FLEET` caps header
- [ ] Repo groups, **collapsible** (▾/▸), with `+rev +agt` ops on the row
- [ ] Work rows: ▾/▸, ⌥ glyph, branch name; meta right-aligned mono:
      `pr#N`/`#N` steel · `±d` `?u` amber · `↑a↓b` green · `N⚑` amber;
      grey when agentless (parked stays visible)
- [ ] Agent rows: LED (green working / amber stale) + name + `MIC` amber
      tag on holder + state word
- [ ] Page rows: glyph (◈ overview · ▦ screen · ± diff · ▤ files ·
      ¶ doc · ❯ terminal) + name + rev
- [ ] **SESSIONS group**: repo-less agents as bare rows (LED, name,
      queue `qN` amber, staleness)
- [ ] `connect → voco attach …` footer (opens connect modal)
- [ ] Click work row = view only; click agent = mic moves (invariant:
      nothing else ever moves the mic)

### 3. Work canvas (center)
- [ ] Tab strip: open pages as tabs `<glyph> name@rev ✕`, active = b0 +
      steel top inset; `+` opens review picker
- [ ] Page bar (second line): `<repo>/<work>` · `<type> · rev N · pushed
      HH:MM:SS by <agent>` · right actions: annotate hint · export ·
      expand-all (diffs) · live/kill (terminals)
- [ ] Doc view (markdown): block hover affordance, **click block or
      select text → editor**; existing annotations = amber wash +
      `A<n>` marker; works for BOTH ¶ docs and ▦ agent screens
      (fixes the dead-click bug — root-cause the screen/doc split)
- [ ] Diff view: collapsed-by-default file index, smart auto-expand
      (open annotations / changed-since), row treatment per mockup
      sample (sign colored, faint bg, amber inset bar on flagged rows),
      interdiff "changed since rN", line-click → editor
- [ ] Files view (▤): **directory tree + syntax highlighting** per
      mockup sample (this IS the diff-annotate file-viewer parity item;
      keep confined tracked-only /v1/file contract)
- [ ] HTML/artifact pages (B1b): sandboxed iframe render + element
      annotate toggle survive the reskin; `da:` deep links keep working
- [ ] Overview page (◈) restyled to tokens
- [ ] Terminal pages (❯): frozen entrypoint stays frozen; existing
      xterm render restyled minimally

### 4. Annotation editor (one component everywhere)
- [ ] Flat form: `ANNOTATE <target> · anchors survive edits · esc ✕`
      bar / textarea / kind segments (concern|question|nit, red inset
      on active) / `☐ blocking` / `cancel` `add ⏎`
- [ ] Same editor for: doc block, doc selection, diff line/range, html
      element (file lines when file annotation lands later)
- [ ] Anchors unchanged: `{exact,prefix,suffix,start,end}`, re-anchor,
      stale-not-dropped

### 5. Channel rack (right, 220px)
- [ ] `CHANNELS — MIC PATCHES HERE` caps header
- [ ] Live channel: amber inset bar, LED, NAME, state+age (`working
      42s`), `MIC` patch (amber when patched), level meter (green→amber
      segments, driven by turn/speech events), live caption line
- [ ] Idle/stale channels: two lines only — LED, NAME, staleness age;
      `MIC` patch + `queue N` amber. No meter.
- [ ] Patch click = explicit mic move (same invariant as tree)
- [ ] Master block: `ptt · duplex` · `attention` (amber value) ·
      `working / listening` counts

### 6. Console (bottom, full width)
- [ ] Tabs with counts: `annotations N · transcript N · asks N · log`;
      right footer: `N open · N blocking` · `export ↓` · `scope: <work>`
- [ ] Annotations table: KIND (+ ⚑ folded in) / TEXT / ANCHOR / ✕;
      resolved = struck row; row click = reveal at anchor; ✕ withdraw
      with undo toast
- [ ] Transcript: radio log (timestamp · speaker · mono text), karaoke
      highlight on the speaking entry, "full" jump from caption
- [ ] **Asks tab: the orphaned W2 loop gets its UI** — list open asks
      (who, question, age), inline answer input, answer → existing
      daemon ask/answer commands, ask clears on answer
- [ ] Log: daemon events feed (quiet, t2)
- [ ] Scope follows selection (work row / agent per the old scoping table)

### 7. Status line (24px)
- [ ] `● daemon 7777` · `MIC → HOLDER` · `ptt_only · half_duplex` ·
      `N working · N listening · N ann` · right: clock + sync note

### 8. States (mockup-silent, tokens govern)
- [ ] Disconnected: command-bar LED red, "reconnecting — retry in Ns",
      surfaces dim read-only, persist error toast
- [ ] Empty states, page-type aware: annotations console ("click a
      block or select text" on docs; "click a diff line" on diffs),
      empty tree group, no channels ("connect →"), empty asks
- [ ] Toasts: errors persist w/ server message; success 4s; undo toasts
      for withdraw/detach
- [ ] Keyboard: esc closes editor/modal · enter submits · visible focus
      ring · F13/hold PTT · ⌘K reserved (palette, later milestone)
- [ ] Thin dark scrollbars; `prefers-reduced-motion` stills meters
- [ ] Modals (review picker · spawn · connect · settings) restyled to
      tokens: b1 surface, line2 frame, caps title bar

## mk3.1 addenda (proposed by builder, captain may veto)

1. Tab ✕ shows on hover/active only (rest state loses 3 ✕ of noise).
2. Asks are amber needs-you events: asks tab count turns amber when >0,
   the asking agent's LED turns amber, status line shows it.
3. `attention` value in the master block is click-to-cycle
   (muted→wake→always), replacing the old orb's click; capture state
   shows as the live meter + `▮` in the prompt.
4. Instrument floor: custom scrollbars, focus rings, reduced-motion —
   built in from M0, invisible until needed.

## Code-reading facts (recorded before M0; keep current)

- Client seam is clean: `store.mjs` (typed slices + subscribe-by-kind),
  `bus.mjs` (WS), per-zone render fns in `app.mjs`. The rebuild keeps
  the store/bus seam and the render-discipline patterns (fingerprint
  gate, scroll memo, async tokens, editor-owns-the-center).
- **Screen-annotation bug root cause (M4)**: `app.mjs` `renderPage`
  routes `type === "doc"` through `renderDocView` (annotation wiring)
  but `type === "screen"` through plain `renderMarkdown` — agent-pushed
  markdown was never annotatable. Fix: screens render through
  `renderDocView` too (annotatable unless params say otherwise).
- Asks plumbing already exists client-side (`store.asks`, `ask.created`
  / `ask.answered` events, `asksFor`, `loadAsks`) — M6 is UI + the
  answer command wiring; confirm ask direction/reply verb in
  PROTOCOL.md before building.
- Canvas tabs need NO new state: tabs = the selected work's open pages
  (what the old rail page-rows showed), `✕` = `page.close`, pinned
  pages get no ✕, overview renders when no page is selected.
- Function homes for old presence-strip controls (inventory addition):
  `■ interrupt` (barge-in) → command-bar keys cell; speaking stop →
  the speaking agent's channel; `attention`/`duplex` (mic.set) → rack
  master block (M5; interim: small selects stay in the keys cell so
  function never disappears); patience presets → settings modal.
- Panel-resize grips persist (localStorage): keep for tree/rack widths,
  add one for console height.
- Light-theme media block in styles.css is removed with the token swap
  (light theme is parked; re-derive from tokens when it returns).
- Old `ui.html` debug client at `/` is untouched — the deck shell is
  `workbench.py:SHELL` loading `static/app.mjs`.

## Milestones (each ends browser-verified vs design/index5 side-by-side)

- **M0 — tokens + shell**: new styles.css token block, the 4-row grid,
  zone containers mounted, old layout retired behind the new frame.
- **M1 — command bar + status line** (live daemon/mic/count data).
- **M2 — fleet tree** (groups/works/agents/pages/sessions, collapse,
  colored meta, selection + mic invariant intact).
- **M3 — canvas tabs + page bar** (open-page tab state, provenance).
- **M4 — views + editor**: doc/screen annotation (incl. the dead-click
  fix), diff, files tree + syntax highlighting, html pages, editor form.
- **M5 — channel rack** (patches, meters, captions, master block).
- **M6 — console** (annotations table, transcript + karaoke, **asks
  UI**, log).
- **M7 — states + modals** (disconnected, empties, toasts, keyboard,
  scrollbars, modal reskin).
- **M8 — final pass**: side-by-side vs mockup, captain click-through,
  journal + docs/BACKLOG.md updated honestly.
- **M9 (post-skin, queued)**: PR/issue attach end-to-end UI · ⌘K
  palette · file-line annotation.

## Working rules

- Builder works alone (no subagents), directly on `workbench`.
- Commit per milestone, message style `feat(deck): M<n> — <what>`.
- Gates per commit: pytest · mypy · ruff check/format · tsc · protocol
  drift when touched.
- Verify against OWN daemon instance on a spare port with hermetic
  state (`[state]` dir + `VOCO_CACHE` overridden) — never against the
  captain's :7777 daemon.
- Server changes only where a feature demands them (asks list/answer
  already exist; prefer client-only).
- Journal below, newest first; docs/BACKLOG.md updated as items land.

## Journal

- **2026-07-09 · ADR-0003 SHIPPED — selection is routing (+ 🔒 lock).**
  The captain suggested this model earlier and was right; the builder
  defended the old rule first — for the record. Sober-analysis
  supersession of the explicit-mic invariant (adr/0003): clicking an
  agent ANYWHERE (tree row, deck card) views + routes — eye contact;
  the master card's mic row toggles a lock ("follows selection" ↔
  "locked 🔒", mirrored as `route → X 🔒`) that makes agent clicks
  view-only for split attention; explicit movers override (per-card
  patch — now shown ONLY while locked, ⌘K mic→ forces, spoken switch);
  daemon-initiated mic moves pull selection along unless locked
  (symmetric via a sessions watcher on activeSession); mic follows
  PEOPLE never places (derived work-row focus stays view-only). Also
  this session: stable card order (no mic-first jumping — channels
  never move; c2b05e7) and the viewed-card steel marking + deck
  selection subscription (a395955). Verified live on :7911, both
  states + override patch. Gates: 374 pytest · tsc. Lock is ephemeral
  by decision (persist only if real use asks).

- **2026-07-09 · mk4 SHIPPED — THE DECK + the right ledger (the swap).**
  Captain-proposed layout revision, mocked as design/index6.html and
  blessed with three notes (all in): no "MIC PATCHES HERE" wording,
  "listening" renders as READY (the state means parked-at-listen; the
  word belongs to the mic — display-only rename, classes/protocol
  untouched), and every card carries its agent's LAST UTTERANCE
  (say_tail, kept live by pushing agent.say events into the store; the
  holder's card shows the live caption instead mid-turn, the speaker's
  card shows the karaoke sentence + ■). Layout: the ledger (annotations
  · transcript · asks · log) moved to a 340px right column beside the
  work — tall content beside the thing it reviews; annotations re-flow
  from the table to stacked rows (kind/⚑/status header with hover
  ✎✕, text, replies, ✓ fixed-in, anchor); footer is its own pinned
  row; transcript/asks revert from the wide-gutter layout to stacked
  (the gutter solved a width problem that no longer exists). THE DECK:
  agents as cards along the bottom, console-strips-laid-flat; dashed
  master card first (hold-to-talk, attention cycle, duplex, counts),
  then mic holder, then by attention order; horizontal scroll for big
  fleets; — / ▢ minimize toggle to a 44px chip strip (persisted,
  voco.deckMin). Card body = view, patch = mic (invariant carried
  over). Gates: 374 pytest · ruff · tsc. Browser-verified on :7911:
  layout, minimize round-trip + persistence, last-say on cards, ready
  wording, reviewed marks still riding the diff heads. STILL OWED:
  #17 scripted smoke (against THIS layout — the wait was right).

- **2026-07-09 · FIT-AUDIT BATCH — export demoted, honest affordances,
  console width.** Captain challenged the export button ("why not send
  to the agent?") — the audit answer: SENDING ALREADY HAPPENS (open
  findings/asks reach the work's agents automatically via the bridge's
  pending-review path), and export is an interop artifact (diff-
  annotate-compatible files for external tools like onebrain3). The
  button was teaching the wrong mental model from the deck's two most
  prominent spots. (1) Export demoted: gone from the page bar and
  console footer; lives on the work overview card ("export review
  file", tooltip says agents already receive annotations live) + the
  palette; the footer's open-count tooltip states the no-send-step
  truth. (2) Attention cycle now includes ptt_only — the old
  muted→wake→always cycle STRANDED a ptt_only daemon after one click.
  (3) Asks carry context: "ask" buttons in the file-selection editor
  and the diff inline editor send ask.create with the selection
  ({kind:file|diff, file, lines, exact…}) and open the asks tab.
  (4) finding.commit renders ("✓ fixed in <sha>" under the text) when
  an agent stamps it. (7) Headless daemons get an honest prompt
  placeholder ("type — routes like speech"). Debug client: already at
  /debug (+ legacy /ui) — kept, no change needed. Console width:
  transcript and asks now use a 160px right-aligned meta GUTTER +
  text column (max 110ch) so the full-width panel reads like a real
  log instead of a skinny column. Gates: 374 pytest · ruff · tsc;
  verified on :7911 (tab identity double-checked first this time).
  STILL OWED: #17 scripted smoke test.

- **2026-07-09 · mk3.1 BATCH SHIPPED — all of A, plus 7/9/10/11-as-
  decided/12/13/14/15.** Bulk pass by file, browser-verified end-to-end
  on :7911. (A1) PR/issue attach: link editor on the work overview card
  — paste `#N` or a GitHub URL, Enter attaches via workspace.link
  (manual wins), ✕ detaches; verified detach `#3` → attach `#42`,
  chips + tree meta updated everywhere. (A2) MINIMAL ⌘K palette
  (palette.mjs): navigation + mic only — works, pages, files, view/
  patch per agent, console tabs, export; ordered-substring fuzzy;
  full action palette deferred. (A3) file-line annotation: select code
  in the files source view → editor → PAGE-LESS finding (server:
  Finding.page_id is now `str | None`, gated to `kind:"file"` anchors,
  rev 0, tests added; workbench handler no longer smuggles the string
  "None"); anchors render `▤ path:LN` and reveal back into the file.
  (A4) real timestamps: Page.updated_ts already existed server-side —
  the client store dropped it; pgbar now shows "pushed HH:MM:SS", and
  channel ages read the event envelope's ts stamped on real state
  transitions (observed-age stays the fallback). (A5) an unseen
  ask.answered pulses the asks tab amber. (#7) hold-PTT: daemon
  ptt.press/release commands ride the EXACT native-hotkey path
  (voice.ptt_press/release; honest "no voice loop" error headless;
  PROTOCOL.md regenerated, 33 commands); rack master "● hold to talk"
  button (module-level held-state + document pointerup so mid-hold
  re-renders can't strand the mic open) + Space-hold in ptt_only;
  verified honest-disabled on the headless verify daemon — LIVE
  audio-path verify needs the captain's real daemon. (#9) ✎ in-place
  edit in the annotations table (finding.update). (#10) diff syntax
  highlighting: lazy per opened file, 800-row cap, hljs. (#11 decided:
  NO MODE) channel body = view (new focusAgent — selection without
  switch_session), MIC patch/tree row/speech = the only mic movers.
  (#12) tree badges split: `N⚑` findings · `?N` asks. (#13) status
  segments act: MIC → view holder; ann count → annotations tab.
  (#14) persistence: group folds, work expansion, files-tree dirs
  (per workspace), console scroll per tab+scope. (#15) diff j/k walks
  change blocks (opens folds, blinks) + ✓ mark-file-reviewed per
  page@rev (persisted; reviewed files fold + dim). Ops lesson recorded:
  chrome-devtools-axi had silently switched to the CAPTAIN's :7777 tab
  — half a verification round ran against the wrong tab/old server;
  always confirm `location.host` before trusting browser evals.
  Deferred list moved to docs/BACKLOG.md "mk3 aftermath". Gates: 374
  pytest (3 new) · mypy · ruff+format · tsc · PROTOCOL regen. NEXT:
  #17 scripted browser smoke test, then B2-18 autostart / B2-20
  notifications when the captain calls them.

- **2026-07-09 · M8 (builder half) — final sweep; captain click-through
  pending.** Side-by-side vs design/index5 on seeded state: layout,
  tokens, tree, tabs+page bar, doc/diff/files views, editor, rack, and
  console all match the pinned mockup; every milestone was
  browser-verified on its own hermetic daemon as it landed. Deliberate,
  journaled deviations (all honest-signal): no ⌘K hint until the
  palette exists; no PTT key hint until ptt.press/release; no page push
  timestamps or fabricated channel ages (protocol carries neither —
  ages appear once a transition is observed live); one amber
  (mk3 folded --warn into --voice). REMAINING for the captain's
  click-through + M9: live-verify an agent ANSWERING an ask (needs a
  real bridge agent); PR/issue attach end-to-end UI (workspace.link
  exists; the connect/link surface is M9's first item); ⌘K palette;
  file-line annotation from the files view; server-side `updated_ts`
  on pages so provenance can carry push time. Captain's daemon on
  :7777 restarted onto the finished build (one hard refresh needed for
  the OLD tab; every restart after that self-heals via the M7
  stale-token reload).

- **2026-07-09 · M7 SHIPPED — states + modals + the restart self-heal.**
  Disconnected is now fully designed: command-bar LED goes red and the
  host cell counts down honestly from the bus's real retryAt ("daemon
  unreachable — retry in Ns"), surfaces + console dim read-only.
  BIG one found by the live drill, not by reading: the wb token is
  minted PER DAEMON BOOT, so a restart stranded every open tab in a
  forever-retry loop with a dead token. Fix in bus.mjs: a socket that
  dies without ever delivering a snapshot while HTTP still answers is
  the stale-token signature → reload once (fresh shell = fresh token),
  sessionStorage-stamped so it can never loop; if reload didn't help
  the bar says "daemon restarted — reload this tab". Drilled live:
  kill + restart the daemon → the tab self-healed to green within one
  retry cycle, zero clicks. (Daemon restarts were THE recurring pain
  this whole build week — old tabs silently running old code; that
  failure mode is dead for connected tabs.) Console got a height grip
  (--dockh, voco.dockh; grip() generalized to a vertical axis + target)
  and modals got the mk3 frame (edge2 border, mono title). Keyboard/
  focus/reduced-motion floor was already in from M0. Gates: 371 pytest
  · ruff · tsc. Settings modal opened + Esc-closed live. NEXT: M8 —
  final side-by-side vs design/index5 + captain click-through.

- **2026-07-09 · M6 SHIPPED — console body: annotations table ·
  transcript · ASKS · log.** renderDock rebuilt as the mk3 console
  (findings.mjs retired; its withdraw-with-undo and reveal behavior
  moved into the table): tabs with counts (asks count amber when a
  question is outstanding — mk3.1 #2 as re-scoped below), footer
  `N open · N blocking · export ↓ · scope`. Annotations = a real table
  (KIND+⚑ / TEXT / ANCHOR / ✕): kinds colored, non-open status rides
  under the kind, agent answers/notes ride under the text, done rows
  struck, row click reveals at the anchor, ✕ withdraws with undo.
  Empty state is finally page-type-aware ("click a block or select
  text" on docs/screens — the reported wrong-hint wart is dead).
  ASK DIRECTION CLARIFIED from the protocol: asks flow YOU → AGENT
  (`ask.create` control command; agents answer via /v1/bridge/ask_reply
  → ask.answered; open ≡ unanswered) — so the asks tab is a composer
  ("? ask <agents>…" → ask.create) plus the thread of your questions
  with answers rendered inline (markdown, sanitized); "needs-you"
  amber = outstanding questions. Log tab: a bounded (300) ring of every
  bus event via a new `onEvent` tap in bus.mjs — ts · seq · type ·
  payload tail, autoscrolled. Verified live on :7911: composer
  round-tripped a real ask (count 1→2, waiting state), log tails
  snapshot + ask.created, table renders all finding states. NOT yet
  live-verified: an agent ANSWER arriving (needs a real agent on the
  bridge; the ask.answered→store path is the same last-writer-wins as
  findings) — M8 covers it. Gates: 371 pytest · ruff · tsc. NEXT: M7 —
  states + modals.

- **2026-07-09 · M5 SHIPPED — channel rack + the final layout.** New
  rack.mjs: agents as channel strips (LED · name · state, with ages
  shown only once a transition is OBSERVED client-side — no fabricated
  durations; the protocol carries no state timestamps). The mic is a
  patch: amber MIC on the holder, clicking an idle channel's patch is
  exactly selectAgent (the one mic-mover; verified live — patching Iris
  updated live channel, cmd-bar route, and status line together). The
  live channel runs the machinery: level meter (animates on
  capturing/holding/routing or speech), live caption (ticker /
  last-routed + full); a speaking agent's channel gets the karaoke
  sentence + ■ stop wherever the mic is. Master block: duplex ·
  attention (click-cycles; honest "headless" when no voice loop) ·
  working/listening. The GRID flipped to its final mk3 shape: mid =
  tree | canvas | rack (rack grip, voco.rackw), and the dock moved to
  the full-width bottom console row (M6 rebuilds its body — functions
  intact meanwhile). Command bar slimmed to final form ([voco ● host]
  [prompt·route] [■ ⚙]); interim caption/speaking/attention cells and
  their CSS removed; offline dim now covers the console row too.
  Gates: 371 pytest · ruff · tsc. Browser-verified on :7911. NEXT: M6 —
  console body (annotations table · transcript · ASKS UI · log).

- **2026-07-09 · M4 SHIPPED — views + editor: the annotation fix, files
  tree + syntax highlighting.** (1) THE FIX: `renderPage` now routes
  `screen` pages through `renderDocView` exactly like `doc` pages —
  agent-pushed markdown was rendering through plain `renderMarkdown`
  with no annotation wiring, which is why block-click did nothing on
  the captain's "strip status" page. Server needed no change
  (finding.add is untyped; the annotatable param gate still applies).
  Verified end-to-end on :7911: block-click opened the editor on a
  SCREEN page and a committed finding round-tripped into the ledger.
  (2) Files view: flat list replaced by a collapsible directory TREE
  with single-child chains compressed ("src/voco/server/static/"),
  lone-root auto-open, filter still giving B1c's flat hit list; source
  view now syntax-highlights. (3) Vendored highlight.js 11.10.0
  (BSD-3-Clause, cdn-assets common build) per the vendor pattern —
  CodeMirror stays out (the reference loads it from a CDN; voco's
  no-CDN/CSP posture forbids that, and hljs is the reference's own
  degrade path). Gotcha recorded in MANIFEST: the build is a classic
  script, so `;globalThis.hljs = hljs;` must be appended post-download
  or the module import silently yields undefined (caught live —
  "highlighted:false" — not by gates). mk3 hljs palette mapped to
  tokens; doc code fences highlight through the same seam
  (markdown.mjs `highlightCode`). (4) Annotation editors (doc + diff
  inline) reskinned to the mk3 flat form via CSS only — structure and
  anchor mechanics untouched. (5) HTML artifacts survive-checked:
  sandboxed iframe (allow-scripts) + annotate toggle render under the
  new canvas. Gates: 371 pytest · mypy · ruff+format · tsc. NEXT: M5 —
  channel rack.

- **2026-07-08 · M3 SHIPPED — canvas tabs + page bar.** The crumb head
  is gone; the canvas now opens with the mk3 duo: a tab strip (the
  selected work's open pages as tabs — no new state, the tree's page
  rows mirrored; pinned-first order; `@rN` on republished pages; ✕ on
  hover/active only per mk3.1 #1; `▤ files` pseudo-tab; `+` opens the
  review picker) and a page bar carrying provenance (`repo/work · type
  glyph · rev N · by <agent>` — no fabricated timestamp: PageMeta has
  no push time; server-side `updated_ts` filed as a post-skin nicety),
  the interdiff since-rev note, a page-type-aware annotate hint (block/
  select on docs+screens, line on diffs, toggle+element on html — the
  page-bar half of the wrong-hint bug), the diff expand-all + stats
  actions, terminal live/kill, and an export button. workFingerprint
  now includes every open page's id:rev so new/republished/closed pages
  rebuild the tab strip even when the selection didn't change. Layout
  hardening from live verify: pgbar children are flex:none with only
  the provenance shrinking (ellipsis) and the bar scrolls horizontally
  — squeezed nowrap flex children paint over their neighbors otherwise.
  Gates: 371 pytest · mypy · ruff · tsc. Browser-verified on :7911:
  tabs render and switch (screen page provenance "· ▦ screen · rev 1 ·
  by Freya"), ✕ hover behavior, narrow-width overflow behavior.
  NEXT: M4 — views + editor (screen-annotation fix, files tree +
  syntax highlighting, html survive-check).

- **2026-07-08 · M2 SHIPPED — fleet tree.** renderRail rebuilt as the
  mk3 tree: FLEET caps header; repo groups collapsible (▾/▸ on the
  group row, in-memory fold state) with `+rev +agt` ops replacing the
  per-group review button and "＋ agent in a new worktree…" action row;
  work rows are ONE line (caret · ⌥ · branch label · right-aligned meta
  cluster: issue/PR chips steel, ±dirty ?untracked gray with the full
  git tooltip, ↑↓ green, open-flag count amber ⚑) with client-local
  expansion (selected work always expanded; carets hold others open);
  agents render as LED child rows — the compact single-agent inline
  form is retired — with amber `MIC` replacing the ⚡ bolt and `qN`
  amber queue; strays group renamed SESSIONS, membership now by
  identity (wsOf) so folded groups don't leak agents into it. LED/state
  semantics remapped to mk3: green = working/listening (live), amber =
  stale, red = blocked, gray = idle. The diff file sub-tree LEFT the
  tree per the pinned decision — the diff view's own file index owns
  per-file navigation (diffSubTree + its cold-load renderRail hook
  removed). Gates: 371 pytest · ruff · tsc. Browser-verified on :7911:
  render matches index5; caret expand, group fold/unfold, and selection
  exercised live; console clean. NEXT: M3 — canvas tabs + page bar.

- **2026-07-08 · M1 SHIPPED — command bar + status line.** presence.mjs
  rebuilt as the mk3 command bar (mount-once kept; input value/focus
  survive renders): [voco ● host] cell with daemon LED (red +
  "reconnecting…" absorbs the offline state), the ONE input behind a
  steel `>` with `route → <holder>` in amber, and a keys cell carrying
  the attention word (click-cycles muted→wake→always — the old orb's
  function; master block takes over in M5), ■ interrupt, ⚙ settings.
  The orb is gone. Interim cells (collapse when empty, move to the rack
  in M5): live caption (bars + ticker / last-routed + full + →route)
  and agent-speaking (eq + who + sentence + full + ■ stop). Status line
  restyled to 10.5px mono: conn LED cell, amber `MIC → name`,
  attention·duplex, counts with emphasized numbers, open-annotation
  count, and a live clock (one persistent node on a 1s interval, never
  a re-render). Honest-signal deviations from the mockup, by decision:
  no ⌘K hint until the palette exists (M9); no PTT key hint until
  ptt.press/release lands; no "rev-sync ok" (nothing measures that) —
  the clock stands alone. Gates: 371 pytest · mypy · ruff · tsc.
  Browser-verified on :7911. NEXT: M2 — fleet tree.

- **2026-07-08 · M0 SHIPPED — tokens + instrument floor.** styles.css
  token block swapped to mk3: gunmetal b0–b3 (+ --hov, --edge2), steel
  #6ea3d8, amber #d9a334 (--warn folded into amber per mk3's one-amber
  rule), green/red signals, system-font stacks. Light-theme media block
  removed (parked; re-derive from tokens when it returns). Instrument
  floor from addendum 4: thin dark scrollbars global, .caps micro-label
  utility; focus-visible + reduced-motion already existed. Zero
  structural change by design — every component inherits the palette
  and the deck stays fully functional; zones land M1–M7. Gates: 371
  pytest · mypy (47 files) · ruff check+format · tsc. Browser-verified:
  hermetic daemon :7911 (scratch state/data dirs, --no-audio) +
  seed_demo; all five zones render in the new tokens, diff/annotation/
  transcript surfaces intact. NEXT: M1 — command bar + status line.
