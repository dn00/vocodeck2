# BACKLOG — audit of 2026-07-08 + the implementation list

Source of truth for what is BROKEN and what is MISSING, from the
code-reading audit (deck client + daemon vs the diff-annotate reference
at `~/Code/claude-setup/skills/diff-annotate` and vs daily-product use).
Work through it top to bottom; every UI item ends browser-verified —
green gates alone have twice claimed victory on a deck you couldn't
scroll.

## B0 — BROKEN (root-caused; fix first)

1. **Center pane cannot scroll.** `styles.css` `.work` is a grid child
   with no `min-height: 0` / no overflow → its auto minimum height is
   its CONTENT height, `.view`'s `overflow: auto` never engages, and
   `body { overflow: hidden }` clips the rest. Rail/dock scroll only
   because setting `overflow` zeroes a grid child's min-height. Also
   the real face of "markdown viewer broken" (long docs render, then
   clip). One line. Broken since U1 — no gate ever scrolled a page.
2. **Scroll-memory keying bug (U2R regression).** `renderWork` saves
   the outgoing view's scrollTop under the NEW selection's pageKey —
   page A's offset restores onto page B ("stuck at the bottom", lands
   mid-document). Save under the key the view was RENDERED for.
3. **Diff rows painted whole-line green/red.** Global `.add/.del
   { color: … }` (meant for stat chips) also matches `.drow.add/.del`.
   Scope the chip classes; rows keep ink text + colored sign + faint bg
   (mockup contract).
4. **Verified NOT broken** (for the record): vendored marked/DOMPurify
   load correctly under browser module semantics (a node test is a
   false negative — node treats `.js` as CJS); CSP permits them; doc/
   screen content shapes match. The markdown renderer was never the
   bug.
5. Small defects: `contentCache` never evicts; `expandSync` can point
   at a stale diff API (label-only); dock rebuilds drop hover state
   (full panelization pending); `selectWork` re-picks the first page on
   re-click of the selected row.
6. **Browser-verified pass** once 1–3 land: drive every scroll surface
   (long diff, long doc, transcript, settings) with the Chrome
   extension or a user click-through. No "fixed" without pixels.

## B1 — REFERENCE PARITY (port from diff-annotate CODE, not memory)

7. **Doc annotation** — `doc-review-panel.mjs` + server `anchor.mjs`:
   annotate by text selection OR plain block click (p/li/h*/pre/quote/
   cell); anchors are `{exact, prefix, suffix, start, end}`, re-anchor
   after edits, stale-not-dropped on re-push. Extend voco findings
   anchors + export mapping accordingly.
8. **Shared reveal machinery** — port `reveal.mjs` (flash, findByText,
   cssEsc); replace the per-surface ad-hoc jump/blink code.
9. **HTML page type** — TODAY IMPOSSIBLE in voco (server 501s unknown
   types). Port: sandboxed iframe (allow-scripts + allow-same-origin),
   `artifact-annotator.js` shim → element findings by CSS path + quote,
   path/content/url modes (url mode = view-only), `da:` deep links
   (da:diff/file:line · da:doc/name:text · da:section/name), confined
   serving + CSP frame-src. Bridge/MCP ingest verb rides page_push.
10. **Rail git status** — port `gitstatus.mjs` semantics: dirty /
    ahead-behind / branch surfaced per work row (the missing "git
    info").
11. **Command palette (Ctrl+P)** — reference `palette.mjs`; un-park.
12. **File explorer + source viewer** — reference `file-viewer.mjs` +
    explorer/files panels: browse tracked files, read any file,
    `file` findings.
13. **`annotatable: false` param** — read-only doc/html sections.
14. Parked pending demand (their own PAGE-TYPES rule: types graduate
    when hand-built twice): contract panel, decision ledger, test
    evidence (lcov), composite pages.
15. Deliberate divergences, NOT gaps: no editor tabs (rail-only nav is
    the deck's pinned IA); findings are server-authoritative (stronger
    than the reference's localStorage-first).

## B2 — PRODUCT LOOPS (the daily-drive P0s)

16. **Uncommitted-work diff source** — `{worktree: true}` → `git diff
    HEAD` in resolver + picker. Branch diffs are merge-base..HEAD =
    committed only; an agent mid-task is invisible today. Biggest
    daily payoff per line of code.
17. **Asks visible + answerable in the dock** — they are counted but
    never rendered (chat tab died in U1; the W2 loop is daemon-complete
    and UI-orphaned).
18. **Daemon autostart** — `voco up` + launchd agent + Tier-0
    first-run (platform detect, model download) per ADR-0002.
19. **Hold-PTT** — daemon `ptt.press/release` + orb hold + keyboard
    hold (the one long-named U1 deferral).
20. **OS notifications** on blocked / needs-you (was parked; daily use
    says otherwise).
21. **Edit annotation** in the dock (finding.update exists server-side;
    no UI).

## B3 — POLISH

22. Settings: curated per-key help copy, "this browser" section
    (theme, panel-size reset), read-only rows for derived values.
23. Diff comforts: syntax highlighting, j/k + mark-reviewed,
    reply-on-finding from the dock.
24. Reconnect countdown ("retry in Ns") in strip + status.
25. Light theme audit + contrast pass; reduced-motion verify (U3).
26. Group header "no agents" meta; content-cache eviction; dock
    panelization (hover-state survival).

## Verification discipline (why this file exists)

The deck shipped two "complete" slices while its center pane could not
scroll. Gates (pytest/mypy/ruff/tsc) prove the machine; only a driven
browser proves the product. Every B0/B1 item lands with either a
Chrome-extension drive-through or a user click-through named in the
journal.
