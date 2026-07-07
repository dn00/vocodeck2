# DESIGN-DECK — UI/UX re-architecture, rev 4 FINAL (STATUS: awaiting the yes)

Final revision, 2026-07-07. Four review rounds with the user (rev 1 →
40%, rev 2 → 50-60%, rev 3.x → 80%, rev 4 = the agreed final scope: the
quiet pass + policies). The rev-by-rev changelog lives in git history;
this document is the pinned spec. Interactive mockup:
`DESIGN-DECK.mockup.html` (self-contained), published at
https://claude.ai/code/artifact/29d18572-3742-46d6-bb7f-4e3c6d9cc7d0

**Nothing here is built. On the user's yes: U0 starts (protocol only).**

## Thesis

The center is the WORK. Voice is a permanent glanceable PRESENCE, not a
chat log. One selection — the agent — and every surface follows it.
Review is a place you go (agentless, diff-annotate parity). Exactly one
free-text input exists and it means "say this".

## The design system (codified — this block lands atop styles.css in U1)

- **Layers, not lines.** Separation by background tier (l0 work, l1
  chrome, l2 hover/inputs, l3 active). Borders exist ONLY at the four
  structural edges (strip bottom, rail right, dock left, status top)
  and around modals/toasts.
- **Three type slots.** 13px sans (chrome) · 12.5px mono (content:
  code, paths, transcripts) · 11px sans (micro/meta). Lowercase chrome;
  no letterspaced uppercase.
- **Color budget: monochrome at rest.** Amber = voice-live or needs-you
  (the ⚡ voice-active mark, flagged counts). Blue = current selection
  only. Red = blocked/destructive only. Green/yellow = live state dots.
  Nothing else is colored, ever.
- **Mono = content.** If it's mono you could read it aloud to the
  agent; chrome is sans.
- **The only curve is the orb.**
- **Contrast.** Micro text uses ink2 (≥4.5:1 on l1); ink3 sets
  decorative marks only, never words.
- **Motion.** Orb pulse, listening bars, speaking eq — all voice.
  Nothing else animates. prefers-reduced-motion stills all three.

## Zones

1. **Presence strip** (top, 44px): orb (attention = ring; click cycles
   muted→wake→always, hold = PTT) · caption slot (listening → one-line
   transcript + "full" + route chip) · the ONE input (placeholder
   carries the idle hint) · speaking slot (who + current sentence +
   "full" + ■ stop; click jumps to speaker) · ■ interrupt · ⚙.
2. **Rail** (left): repo groups (name + review button; agentless groups
   say "no agents") → agents (state dot + word, ⚡ voice-active, flagged
   count; blocked sort to top) → pages with type icons (◈ overview ·
   ▦ screen · ± diff · ¶ doc · ❯ terminal) → the diff's file sub-tree
   (stats + finding dot; click = expand + jump). "＋ agent in a new
   worktree" opens spawn.
3. **Work** (center): slim crumb header (Freya / page · rev + page
   actions: expand-all for diffs, live/kill for terminals) + the view.
   Diff = collapsed-by-default file index (smart auto-expand: open
   annotations / changed-since files start open). Annotation editor =
   diff-annotate reference structure verbatim (target label, textarea,
   concern|question|nit pills + blocking, add/cancel, tip line).
4. **Dock** (right): scope header ("Freya · vocodeck2 · workbench") +
   tabs **annotations | transcript** + export. Annotations: flat rows,
   tinted-text tags, ✕ withdraw (undoable). Transcript: radio log —
   timestamp · speaker · wrapped mono text; "full" in the strip jumps
   here and highlights the entry; the speaking entry karaoke-highlights
   live (dim said · blue hearing-now · faint queued).
5. **Status line** (bottom, 24px): ● port · active agent · attention ·
   duplex · state counts (working/listening/blocked) · open
   annotations. Ambient truth only, no controls.
6. **Modals**: review picker (branch/pr/staged) · spawn (harness, repo,
   worktree, tmux/pty) · connect (the only place CLI one-liners appear)
   · settings (config.get/set; hot-apply live, restart keys marked).

## The long-utterance pattern (rev 4 — replaces floating cards)

The strip is the moment (one line); the transcript is the record.
"full" switches the dock to the transcript, scrolls to the entry, and
highlights it — user and agent symmetric, no floating layers, no
overlap, enlargeable because the dock resizes. Agent karaoke lives in
the transcript entry while speaking.

## Interaction policies (written down so they survive)

- **Destructive:** undo over confirm where reversible — withdraw
  annotation and detach agent get "— undo" toasts (detach never touches
  the process). Confirm reserved for kill (irreversible, names what
  dies).
- **Toasts:** errors persist until dismissed and carry the server's
  contextual message; successes auto-fade 4s; one toast per event.
- **Disconnected:** strip says "daemon unreachable — reconnecting,
  retry in Ns"; surfaces dim read-only; status line shows the retry;
  error toast persists. Reconnect re-syncs from the snapshot (already
  server-side).
- **A11y floor:** captions + transcript are aria-live="polite"; state
  never color-only (every dot has an adjacent word); visible focus ring
  on everything focusable.
- **Keyboard floor (U1):** Esc closes modals · Enter submits · focus
  ring · PTT hold key while the browser has focus. Ctrl+P parked.

## Scoping model

| You select… | Center | Dock·annotations | Dock·transcript | Voice+input |
|---|---|---|---|---|
| Agent | her pages (diff vs HER branch/worktree, screen, terminal) | her workspace's ledger | your conversation with her | routes to her |
| Repo (no agent) | its pages + review picker (agentless fully works) | its ledger | "no agent" + connect/spawn | unchanged |

Global exception: sound. If Orion speaks while Freya is selected, the
speaking slot names him (click jumps). Rail chips stay live for all.

## Voice presence — signal map

| element | signal |
|---|---|
| orb ring color/label | `mic.state` |
| orb pulse + bars | `turn.state = capturing` |
| orb steady glow | `turn.state = holding/routing` |
| caption + route chip | `stt.final` + `route.decision` |
| speaking slot + rail halo | `speech.started/finished` (U0: + who, text) |
| transcript karaoke | `speech.sentence` (U0, from `_sentence_synth`'s generator at sentence pull; degrade = whole-message highlight) |
| transcript "queued Ns" meta | `input.queued` |
| live partial words | `stt.partial` — declared, unemitted (batch whisper); UI subscribes now |

## Daemon budget (all of it)

1. `page.publish` control command — human diff publish, daemon-side
   resolution (existing DiffResolver) in the workspace root.
2. `workspace.open` control command — mint a workspace from a real path
   (agentless first-run parity).
3. `speech.started/finished` payloads gain `who` + `text`; new
   `speech.sentence` per-sentence progress event.
4. Per-session user-input log symmetric to `say_log` (deque(50),
   persisted, recorded at `dispatch()`: `{ts, text, origin, queued}`).
   Route/moment data never stored.

Spawn/connect modals need nothing new (`session.spawn` + adapters
exist). Untouched: store/bus seam, pages model, findings/asks
convergence, diff renderer + interdiff, xterm pages, worktrees (W3),
one-selection agent model, export contract.

## Decisions log (user-resolved)

- Forks (2026-07-07): orb click-cycles + hold-PTT · input in the top
  strip · transcript bounded 50/side · diff smart auto-expand + rail
  file sub-tree.
- Naming: dock tab **annotations** (lists things); repo button
  **review** (starts an activity).
- Workspace is data-only; rail groups agents by repo (common_dir);
  custom named groups parked (SPEC decision 20).
- Rejected: messenger-first layout (2026-07-06); floating utterance
  cards (rev 4 — overlap + not enlargeable).
- herdr borrows (concepts only, AGPL): state-count aggregation in the
  status line; blocked-first rail ordering. Parked from herdr: OS
  notifications on state change.
- Parked: Ctrl+P switcher, custom groups, transcript search, voice
  phrase discoverability surface, streaming STT captions (UI ready).

## Build order (each slice ends at a USER CLICK-THROUGH)

- **U0 — protocol:** the four daemon items, tests at the command seam.
  No pixels.
- **U1 — presence + scoping + the quiet skin:** strip, dock
  (annotations|transcript + scope header), rail tree, status line,
  tokens block in styles.css, keyboard floor, disconnected state, toast
  policy; feed strip / old status bar / chat tab / center tabs removed.
  Checkpoint: you talk and see it; she talks and you see it; long
  prompts readable in the transcript; pull the daemon's plug and the UI
  says so.
- **U2 — review as a place:** picker, repo review buttons, collapsed
  file index + rail file sub-tree, reference annotation editor,
  spawn/connect modals, empty states, withdraw-with-undo. Checkpoint:
  diff-annotate parity with no agent running.
- **U3 — polish:** settings modal, persisted panel sizes, light theme,
  contrast audit, reduced-motion verify. Checkpoint: the "GitHub rando"
  walkthrough.
