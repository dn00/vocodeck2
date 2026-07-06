# DESIGN-DECK — UI/UX re-architecture proposal, rev 2 (STATUS: awaiting user approval)

Rev 2, 2026-07-06 — reworked against the user's six redlines on rev 1
("40% there"). **No UI from this document may be built until the user
says yes** (open forks below need answers too). Interactive mockup:
`DESIGN-DECK.mockup.html` in this repo (self-contained, open in any
browser), also published at
https://claude.ai/code/artifact/29d18572-3742-46d6-bb7f-4e3c6d9cc7d0

## Thesis

The center is the WORK. Voice is a permanent glanceable PRESENCE, not a
chat log. **One selection — the agent — and every surface follows it:**
pages, review ledger, chat history, input routing. Voice moments are the
single global exception (sound is unscopeable). Review is a place you go
(agentless, diff-annotate parity). Exactly one free-text input exists,
and it means "say this".

## Rev-1 redlines → rev-2 answers

1. **Long prompts unreadable in the routed caption** → strip stays
   one-line (it is the moment, not the record) + a "⌄ full" expansion
   card with the complete utterance; the full text also lands in the
   Chat tab, which has room and wraps.
2. **History popover weak** → KILLED. History = the **Chat tab** in the
   right dock (user's suggestion), scoped per agent, full-width wrapped
   messages, timestamps, queued markers, survives restarts.
3. **Review panel mixed all agents/repos** → the dock is **scoped to
   the selection** and its header names the scope ("Freya · vocodeck2 ·
   workbench"). Nothing global in the dock, ever.
4. **Right panel could use tabs** → it has them: **Review | Chat**,
   both following the same selection.
5. **Annotation input must match diff-annotate** → adopted verbatim
   from the reference `diff-panel.mjs` + `finding-controls.mjs`:
   inline editor row under the selected line, "Concern for file:line"
   target label, textarea, pill row Concern|Question|Nit + blocking
   checkbox, "Add annotation"/"Cancel" buttons, the shift-click-range +
   Ctrl/Cmd+Enter tip line.
6. **No settings surface** → gear in the strip → settings modal over
   the existing `config.get`/`config.set`, honest about hot-apply vs
   restart-required (daemon already reports which); client prefs
   (theme, panel sizes) included.

## The scoping model

| You select… | Center | Dock·Review | Dock·Chat | Voice+input route |
|---|---|---|---|---|
| Agent | her workspace's pages (diffs vs HER branch/worktree, screen, terminal) | her workspace's ledger | your conversation with her | her (voice-active) |
| Workspace (no agent) | its pages + review picker (agentless works fully) | its ledger | "no agent here" + attach affordance | unchanged |

Global exception: the presence strip always tells the truth about sound
— if Orion speaks while Freya is selected, his name/words show in the
speaking slot (click to jump to him); rail chips stay live for all
agents.

## Zones

1. **Presence strip** (top): orb (attention = ring color; pulses on
   `turn.state=capturing`; click cycles, hold = PTT) · caption slot
   (listening → transcript one-liner + "⌄ full" card + route chip) ·
   the ONE input · agent-speaking slot (who + sentence + ■ stop, click
   to jump) · interrupt · gear · connection. Status bar and bottom
   feed strip are REMOVED.
2. **Rail** (left): agents (one-selection model; speaking halo), then
   repos (each with a standing Review button; + new-worktree).
3. **Work** (center): tabstrip + pages, unchanged in role; `⊕ review`
   affordance; the reference annotation editor; product empty states
   with working buttons.
4. **Dock** (right): scope header + tabs **Review | Chat** + export.
5. Modals: review picker (branch/PR/staged), settings.

## Voice presence — signal map (no invented state)

| element | signal |
|---|---|
| orb ring color/label | `mic.state` |
| orb pulse + listening bars | `turn.state = capturing` |
| orb steady glow | `turn.state = holding/routing` |
| caption + route chip + expansion card | `stt.final` + `route.decision` |
| speaking slot + rail halo | `speech.started/finished` (needs who+text enrichment) |
| chat "queued Ns" marker | `input.queued` |
| live partial words | `stt.partial` — declared, UNEMITTED (batch whisper); UI subscribes now, lights up when streaming STT lands |

## Daemon support required (the whole complexity budget)

1. **Per-session user-input log** — the chat tab's missing half. Agent
   half exists (`say_log`, deque(50), persisted). Symmetric addition:
   record dispatched transcripts on the session at `dispatch()` —
   `{ts, text, origin: voice|typed, queued: bool}`, same bound, same
   persistence. NOT stored: route decisions / moment data — the strip
   shows those live and they die with the moment.
2. **`page.publish` control command** — human-initiated publish
   `{workspace, type: "diff", source: {branch|pr|staged}}`, daemon-side
   resolution via the existing DiffResolver in the workspace root (the
   §3.2 sentence that was speced and never built; the bridge verb can't
   do it — it demands a session).
3. **`workspace.open` control command** — mint a workspace from a real
   directory on the daemon host (agentless first-run parity).
4. **`speech.started/finished` payloads gain `who` + `text`** (today:
   only source + turn_id).

Everything else is client-only. Unchanged: store/bus seam, pages model,
findings/asks convergence, diff renderer + interdiff chips, xterm
terminal pages, agent card, worktree spawn, one-selection agent model.

## Build order (each slice ends at a USER CLICK-THROUGH, not gates)

- **U0 — protocol:** page.publish, workspace.open, speech enrichment,
  per-session input log. Tests at the command seam before any pixel.
- **U1 — presence + scoping:** strip (orb/captions/expansion/one
  input/speaking slot), dock tabs Review|Chat with scope header; feed
  strip + status bar + old chat tab removed. Checkpoint: you talk — you
  see it, long prompts readable; agent talks — you see it; Orion's
  findings never appear under Freya.
- **U2 — review as a place:** picker, repo Review buttons, ⊕ tab, the
  reference annotation editor, empty states, agentless flow.
  Checkpoint: diff-annotate parity with no agent running.
- **U3 — polish:** settings modal, resizable persisted panels, type
  split, focus states, reduced-motion, light theme. Checkpoint: the
  "GitHub rando" walkthrough.

## Open forks (user decides before U1)

1. **Orb interaction:** click cycles attention + hold = PTT (proposed),
   vs click = PTT toggle + attention via menu.
2. **Input placement:** top strip (proposed/shown) vs a single bottom
   bar — exactly one exists either way.
3. **Chat depth:** bounded per-agent log (50/side, restart-surviving —
   proposed) vs a fuller durable transcript (bigger bound, export).
   Lean: bounded — it's presence support, not a log system.
