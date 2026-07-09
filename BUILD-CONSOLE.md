# BUILD-CONSOLE — the mk3 UI rebuild (plan + running journal)

**Pinned design: `design/index5.html` (CONSOLE mk3, blessed 2026-07-08).**
The deck client is rebuilt to look exactly like that mockup. Where the
mockup is silent (modals, disconnected, empty states), the tokens and
rules below govern. Style tokens are immutable without captain sign-off.
Design lineage for archaeology: index.html (round 1: bridge/paper/signal)
→ index2 (D·CONSOLE) → index3 (rejected restyle) → index4 (mk2 quiet)
→ **index5 (mk3 final)**.

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
