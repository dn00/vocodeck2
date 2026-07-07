# DESIGN-DECK — UI/UX re-architecture proposal, rev 3 (STATUS: awaiting user approval)

Rev 3, 2026-07-06 — rev 2 was "50-60%"; this revision answers the next
batch of redlines (brutalism, transcript, rail tree, collapsed diffs,
workspace-as-grouping, status line, terminal view, product first-run).
**No UI from this document may be built until the user says yes** (open
forks below need answers too). Interactive mockup:
`DESIGN-DECK.mockup.html` in this repo (self-contained), published at
https://claude.ai/code/artifact/29d18572-3742-46d6-bb7f-4e3c6d9cc7d0

## Thesis

The center is the WORK. Voice is a permanent glanceable PRESENCE, not a
chat log. **One selection — the agent — and every surface follows it.**
Review is a place you go (agentless, diff-annotate parity). Exactly one
free-text input exists and it means "say this". Visual language: tech
brutalism — **the only curve in the deck is the voice** (the mic orb);
everything else is hairline grid, square uppercase mono tags, flat
bordered buttons, no washes, no soft shadows, no border-radius.

## Rev-2 redlines → rev-3 answers

1. **Rounded message cards / "curve bracket" styling** → whole skin
   rebuilt brutalist (rules above). Bubbles gone entirely.
2. **"Chat" naming + messenger worry** → renamed **TRANSCRIPT** and
   restructured as a radio log: flat hairline-separated entries
   (timestamp · speaker tag · wrapped text), one shared column, no
   alternation, no own input. Voice-native metaphor = captions history.
3. **Diffs collapsed by default** → the diff is a file index: header
   rows (path · +/− · finding count · since-rev chip), collapsed by
   default, per-file toggle + EXPAND ALL/COLLAPSE ALL in the work
   header. Files with open findings / changed-since-your-rev
   auto-expand (fork 4 below).
4. **Horizontal center tabs** → KILLED. Pages nest under the agent in
   the rail — the tree IS the scoping model, one navigation axis. The
   center gets a slim context header (crumb FREYA / page · rev +
   page-level actions). Ctrl+P quick-switcher parked for later.
5. **Agent name in page labels** → gone; nesting makes ownership
   structural. Confirmed: selected-agent-per-surface is the model; the
   speaking slot stays the sole global exception (sound is unscopeable).
6. **Workspace concept — needed?** → workspace stays **data-only**
   (findings/pages/manifests key on it; review must survive agents
   detaching). As UI it dies: the rail groups agents **by repo**
   (worktree siblings fold in via `common_dir`, zero-config). A repo
   group with no agents renders "review-only" — the agentless
   workspace, earning its place. Custom named groups parked until
   dogfood demands them (SPEC decision 20: grouping is arrangement,
   never identity).
7. **Bottom status bar** → back, 24px, quiet: division of labor is
   presence strip = voice moments, status line = ambient system truth
   (● live · daemon addr · active agent · attention·duplex · live-git ·
   agent/finding counts · last export). No controls in it.
8. **No terminal UI** → fixed: terminal page view in the mockup — pty
   stream (xterm, ring replay on attach, interactive-on-focus with a
   visible focus tag, kill in the header, 80×24), tmux pages mirror
   with a say-as-user row. Just another page in the agent's tree.
9. **voco commands on first run** → gone from the landing. First run =
   three product buttons: **Spawn an agent** (surfaces the existing
   `session.spawn`: harness claude/codex/custom, repo, optional
   worktree, tmux/pty backend), **Review a diff** (agentless), **Open a
   repo**. CLI one-liners live behind a "connect →" modal for people
   who already run sessions (voice_init / voco listen / mcp add).
10. **Live caption for the speaking agent** (rev 3.1; supportability
   VERIFIED rev 3.2) → symmetry with the user's captions: the speaking
   slot shows the sentence being voiced NOW; FULL ⌄ drops the whole
   response karaoke-highlighted (dim = said, highlight = hearing,
   faint = queued); ■ STOP cuts the rest. Code facts: a say is ONE
   PlaybackItem; sentences are chunked INSIDE it (`_sentence_synth`,
   voice_loop.py:344), so `speech.started` fires per MESSAGE. U0 adds a
   `speech.sentence` event emitted from that generator as the player
   pulls into each sentence (playback-aligned within the audio buffer;
   degrade path = whole-message highlight). The card itself needs only
   `agent.say`, which exists.
11. **REVIEW button everywhere** (rev 3.1) → over-affordance fixed:
   review is repo-scoped, so the button lives ONLY on the repo group
   header (first-run's "Review a diff" stays — it is the landing).
   Work-header ⊕ removed.
12. **herdr borrows** (rev 3.1; concepts only — AGPL, SPEC decision 7)
   → adopted: state-at-a-glance aggregation (status line counts
   `1 working · 1 listening · 0 blocked`) and blocked-is-loud —
   blocked/asking agents sort to the TOP of the rail (attention-first
   ordering, lands in U1). Already ours: real terminals, tmux
   detach/reattach survival, agent-scriptable control API. Its
   workspace/tab/pane arrangement stays parked with custom grouping.
   Worktrees: confirmed supported since W3 (sibling worktree spawn,
   dirty trees never deleted, common_dir rail grouping).

## Rev 3.2 — the no-MORE/no-LESS audit (user at 80%)

Calibration: herdr's discipline (four state colors, one sidebar, real
terminals, nothing else). CUT: "full duplex" under the orb (status line
owns it); repo-group agent/checkout counts (the list says it); the
"review-only" explainer row; "exported hh:mm" status cell (toast, not
status); the ◆ pinned marker (never varies). KEPT deliberately: the
whole presence strip (each element is a distinct live signal), the dock
scope header (cutting it re-opens rev 2's whose-findings bug), the
7-cell status line, ■ interrupt vs STOP (different verbs: kill work vs
cut speech). ADDED (gaps): ✕ withdraw on open annotations
(`finding.withdraw` existed with no UI); ✕ close on page-row hover (U1;
`page.close` exists). PARKED: OS notifications on agent-blocked (herdr
has them; Notification API or Tauri later), Ctrl+P switcher, custom
groups, transcript search.

**Naming (user call, resolved):** dock tab = **ANNOTATIONS** (it lists
things; diff-annotate lineage). The repo-group button stays **REVIEW**
(it starts an activity). You review; you leave annotations.

**Visual:** the orange agent-tree bar is replaced by page-TYPE icons
(◈ overview · ▦ screen · ± diff · ¶ doc · ❯ terminal) — decoration
replaced by information.

**Status tracking (herdr comparison):** herdr = process-name matching +
terminal-output heuristics, no hooks; notifications configurable. Deck
= strictly stronger ground truth — bridge facts first (parked listen =
provably listening; dispatch = provably working; pending ask = provably
blocked-on-you; §6 display_state), pane-watcher heuristics only as the
fallback for unmanaged terminals. Adopted from herdr: the PRESENTATION
(aggregate state counts in the status line, color-block dots,
blocked-first rail ordering).

## Zones (rev 3)

1. **Presence strip** (top): orb · captions (one-liner + FULL ⌄
   expansion card for long utterances) · the ONE input · speaking slot
   (who + sentence + ■ STOP, click jumps to speaker) · interrupt ·
   gear · connection.
2. **Rail** (left): repo groups → agents → pages (selected agent's
   pages nest under them; the tree is the only navigation). Repo group
   header carries REVIEW; "＋ agent in a new worktree" opens spawn.
3. **Work** (center): slim context header (crumb + page actions:
   expand-all for diffs, live/kill for terminals, ⊕ review) + the view.
4. **Dock** (right): scope header ("FREYA · vocodeck2 · workbench") +
   tabs **REVIEW | TRANSCRIPT** + export. Findings = flat rows, square
   tags. Transcript = radio log.
5. **Status line** (bottom): ambient truth, listed above.
6. Modals: review picker (branch/PR/staged), spawn, connect, settings
   (over config.get/set, honest hot-apply vs RESTART marks).

## Voice presence — signal map (unchanged from rev 2)

| element | signal |
|---|---|
| orb ring color/label | `mic.state` |
| orb pulse + listening bars | `turn.state = capturing` |
| orb steady glow | `turn.state = holding/routing` |
| caption + route chip + FULL card | `stt.final` + `route.decision` |
| speaking slot + rail halo | `speech.started/finished` (needs who+text) |
| transcript "queued Ns" meta line | `input.queued` |
| live partial words | `stt.partial` — declared, UNEMITTED (batch whisper); UI subscribes now |

## Daemon support required (the whole budget — unchanged from rev 2)

1. Per-session **user-input log** symmetric to `say_log` (deque(50),
   persisted, recorded at `dispatch()`: `{ts, text, origin, queued}`).
   Route/moment data never stored.
2. **`page.publish`** control command (human diff publish, daemon-side
   resolution in the workspace root).
3. **`workspace.open`** control command (mint workspace from a real
   path — agentless first-run parity).
4. **`speech.started/finished` payloads gain `who` + `text`**, plus the
   **`speech.sentence`** per-sentence progress event (rev 3.2 — emitted
   from `_sentence_synth`'s generator at sentence pull; see redline 10).

Spawn/connect modals need NOTHING new — `session.spawn` + adapters
exist. Everything else is client-only. Unchanged machinery: store/bus
seam, pages model, findings/asks convergence, diff renderer + interdiff,
xterm terminal pages, agent card, worktree spawn.

## Build order (each slice ends at a USER CLICK-THROUGH)

- **U0 — protocol:** page.publish, workspace.open, speech enrichment,
  user-input log. Tests at the command seam first.
- **U1 — presence + scoping:** strip, dock REVIEW|TRANSCRIPT + scope
  header, rail tree (repo groups → agents → pages), status line; feed
  strip + old status bar + chat tab + center tabs removed. Checkpoint:
  you talk — you see it (long prompts readable); agent talks — you see
  it; Orion's findings never under Freya.
- **U2 — review as a place:** picker, repo REVIEW buttons, collapsed
  file index + expand all, reference annotation editor, spawn/connect
  modals, empty states, agentless flow. Checkpoint: diff-annotate
  parity with no agent running.
- **U3 — polish:** settings modal, resizable persisted panels, focus
  states, reduced-motion, light theme. Checkpoint: "GitHub rando"
  walkthrough.

## Forks — DECIDED (user, 2026-07-07)

1. **Orb:** click cycles attention (muted → wake → always); hold = PTT.
2. **Input:** top presence strip.
3. **Transcript:** bounded 50/side per agent, restart-surviving.
4. **Diff folds:** smart auto-expand (open-annotation / changed-since
   files start open) + **the diff's file list nests in the rail tree
   under the diff page** (click a file → expand + jump; U2).

Design pinned at rev 3.2. Remaining input before build: the overall
yes; then U0 starts (tests at the command seam, no pixels until green).
