# DESIGN-DECK — UI/UX re-architecture proposal (STATUS: awaiting user approval)

Written 2026-07-06 per the RESUME-HERE mandate in BUILD-WORKBENCH.md:
design first, code second. **No UI from this document may be built until
the user says yes** (open forks below need answers too). Interactive
mockup: `DESIGN-DECK.mockup.html` in this repo (open in any browser —
self-contained), also published at
https://claude.ai/code/artifact/84be985c-e771-4739-a482-54cf254005a1

## Thesis

The center is the WORK (diffs, docs, screens, terminals). Voice is a
permanent, glanceable PRESENCE — not a chat log. There is exactly one
free-text input in the app, and it means "say this". Review is a place
you go (agentless, diff-annotate parity), not a thing you wait for.

## Field report → design answer

1. Voice invisible → **presence strip** (top, permanent): mic orb
   (attention mode = ring color; pulses on `turn.state=capturing`),
   live caption slot (listening → transcript → route chip), agent-
   speaking slot (who + sentence + ■ stop), history toggle, interrupt,
   connection dot. Status bar and bottom feed strip are REMOVED.
2. Debugger feel → sans chrome / mono content split; real buttons and
   pickers; no `prompt()`/`confirm()`; every empty state carries a
   working action (buttons, copy chips), not just CLI hints.
3. Under diff-annotate → **review picker**: "review" button on every
   repo row + `⊕ review` in the tabstrip → branch/PR/staged picker →
   daemon resolves in the workspace root → diff page opens. No agent.
   (SPEC §3.2 promised this; it was never built.)
4. Two chats → **one input story**: the single input in the presence
   strip routes via `say_as_user` exactly like speech. Questions to an
   agent are ANNOTATIONS (created in context on a diff line/page, read
   in the ledger). The dock chat tab dies; findings + asks merge into
   one review ledger with replies under their cards.
5. No resizing → drag grips on rail and dock edges, sizes persisted in
   localStorage; both panels collapsible.
6. Messenger-first REJECTED → honored: conversation history is a
   summoned drawer from the presence strip, deliberately not dockable.

## Zones

1. **Presence strip** (top): orb · captions · the one input · speaking
   slot · history/interrupt/conn.
2. **Rail** (left): agents (one-selection model kept; + speaking halo)
   then repos (grouped, each with a Review button; + new-worktree).
3. **Work** (center): tabstrip + pages, unchanged in role; adds
   `⊕ review` affordance and product empty states.
4. **Review ledger** (right dock): findings + questions, one list;
   export stays.
5. **History drawer** (summoned from the strip): you/route/say/queued
   lines; never docked.

## Voice presence — signal map (no invented state)

| element | signal |
|---|---|
| orb ring color/label | `mic.state` (muted/wake/always/ptt, duplex) |
| orb pulse + listening bars | `turn.state = capturing` |
| orb steady glow | `turn.state = holding/routing` |
| caption transcript + route chip | `stt.final` + `route.decision` |
| speaking slot + rail halo | `speech.started/finished` (needs payload enrichment) |
| "queued for X" suffix | `input.queued` |
| live partial words | `stt.partial` — declared, UNEMITTED (batch whisper); UI subscribes now, lights up when a streaming STT lands |

## Underlying-logic changes required (protocol-first, all small)

1. `page.publish` control command — human-initiated publish
   `{workspace, type: "diff", source: {branch|pr|staged}}`, daemon-side
   resolution via the existing DiffResolver in the workspace root.
   (The bridge verb requires a session; this must not.)
2. `workspace.open` control command — mint a workspace from a real
   directory path on the daemon host (agentless first-run parity).
3. `speech.started/finished` payloads gain `who` (call name) + `text`
   being voiced (today: only source + turn_id).
4. Everything else is client-only.

## What stays

Store/bus seam, pages model, findings/asks convergence semantics, diff
renderer + interdiff chips, xterm terminal pages, agent card, worktree
spawn, one-selection agent model. Not a rewrite.

## Build order (each slice ends at a USER CLICK-THROUGH, not gates)

- **U0** protocol: page.publish + workspace.open + speech enrichment,
  tests at the command seam first.
- **U1** presence strip + one input + history drawer; feed strip,
  status bar, chat tab removed; ledger merged. Checkpoint: you talk,
  you see it; agent talks, you see it.
- **U2** review-as-a-place: picker, repo Review buttons, ⊕ tab, empty
  states, agentless flow. Checkpoint: diff-annotate parity test.
- **U3** polish: resizable persisted panels, type split, focus states,
  reduced-motion, light theme. Checkpoint: "GitHub rando" walkthrough.

## Open forks (user decides before U1)

1. Orb interaction: click cycles attention + hold = PTT (proposed), vs
   click = PTT toggle + attention via menu.
2. Input placement: presence strip top (proposed) vs single bottom bar
   — exactly one exists either way.
3. History drawer scope: voice-only (proposed) vs interleaved findings
   activity.
