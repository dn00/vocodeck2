# VocoDeck 2 ("voco") — Foundation Spec

Status: v1.1 — grilled with the user, adversarially reviewed (14 findings
applied), ready for build. This document is the build reference for the new
voice-first foundation. It will eventually absorb VocoDeck's UI and carry the
VocoDeck name; until then the components are: **voco-d** (daemon),
**voco-cli** (CLI, aliased `voco`), **voco-mcp** (MCP bridge), directory
`vocodeck2/`.

Reference projects (read before building; steal patterns, verify claims):

- **huggingface/speech-to-speech** — the latency playbook: Silero VAD with
  duration hysteresis, speculative turn handling, streaming STT/TTS, modular
  handler design. We take its *components and constants* (verified against
  the repo, incl. PR-307), not its skeleton (it is a synchronous chat loop;
  we are a switchboard in front of a slow expert).
- **andimarafioti/faster-qwen3-tts** — premium TTS on the Windows/3090 box.
  CUDA-graph decode, streaming, voice cloning, OpenAI-compatible
  `/v1/audio/speech`. Note: its published TTFA numbers (156ms for 0.6B) are
  **RTX 4090 measurements**; expect ~250–400ms on the 3090 until measured.
- **vocodeck (v1)** — the product principles (local-first, fail-silent,
  derive-don't-ask, capability matrix), hexagonal engineering discipline,
  protocol-package-first structure, decision 006 (attach inside-out, managed
  as convenience, no wrappers ever), and the `source.speak`/`source.screen`
  companion vocabulary the future UI expects.

---

## 1. What this is

A local-first voice control plane for coding agents. You talk, hands-free or
push-to-talk; a fully local speech stack (VAD → STT → local tier → TTS) gives
you sub-second audible feedback; your words are routed to whichever attached
agent session is active — Claude Code, Codex, pi, opencode, anything — running
locally, in tmux, or on a remote box over SSH. The agent's spoken replies
stream back through the same pipe. A local Gemma tier ("the first mate")
answers what it's allowed to instantly and operates the switchboard.

### Product principles (inherited + new)

1. **Local-first.** Audio never leaves the machine. Remote sessions exchange
   *text only*, through an SSH tunnel the user already trusts (see §9.1 for
   the shared-host caveat and tokens).
2. **Fail-silent toward the agent.** The bridge must never break an agent
   session. Daemon down → bridge adapters degrade softly (§8.4), never raise
   into an agent turn.
3. **Derive, don't ask.** Session identity (host, cwd, harness, repo, branch)
   comes from transport/process facts supplied by the bridge, never from the
   model.
4. **Capability matrix.** Sessions declare what they support (`say`,
   `listen`, `screen`, later `inject`, `stream_tap`); behavior degrades
   per-cell, never breaks.
5. **First audio never waits on the frontier model.** Every audible response
   comes from a layer at least one tier cheaper than the thing that produces
   the real answer.
6. **Partition of authority.** The local tier and the agent can never
   contradict each other because they are never allowed to speak about the
   same things (§7).
7. **Irreversible actions get the hold; reversible ones start instantly.**
   Local speech can be cancelled, so it starts immediately. Agent dispatch
   cannot be un-sent, so it waits out the hold — and **dispatch closes the
   turn**: once dispatched, later speech is a new utterance (§5.2).

---

## 2. System overview

```
                         ┌────────────────────────────────────────────┐
                         │                voco-d daemon                │
                         │                                            │
 mic ──► capture ──► VAD ──► STT ──► phrase table ──► Gemma contract   │
  ▲        (PTT overrides)   │            │                │           │
  │                      partials     hard cmds       route decision   │
  │                          │      (stop/mute/switch)  + action       │
  │                          ▼            │          ┌─────┴─────┐     │
  │                     WS events ◄───────┘          ▼           ▼     │
  │                          ▲                 local speech   dispatch │
  │                          │                       │           │     │
 spk ◄── playback ◄── TTS queue (prioritized, barge-in) ◄── agent say  │
                         │                                       ▲     │
                         │        session registry               │     │
                         │   ┌──────────┬──────────┐             │     │
                         └───┤ bridge HTTP (long-poll /listen,   │     │
                             │   /say, /screen, /register)  ─────┘     │
                             └──────────┬──────────┘                   │
                         └──────────────┼──────────────────────────────┘
                                        │ localhost:7777 (never 0.0.0.0)
              ┌─────────────────────────┼──────────────────────────┐
              ▼                         ▼                          ▼
        voco-cli / voco-mcp      voco-cli / voco-mcp        future UI (WS)
        (local Claude Code)      (remote Codex, via         (Tauri/React,
                                  ssh -R 7777:localhost:7777) vocodeck port)
```

Three surfaces on one localhost port:

- **Bridge HTTP** (`/v1/bridge/*`): what agents use. Long-poll `listen`,
  fire-and-forget `say`/`screen`. Tunnel-friendly, testable with curl.
- **WS event stream** (`/v1/events`): what UIs and tools observe. Every
  internal state change is an event; connect → snapshot (§10).
- **Control HTTP** (`/v1/control/*`): switch session, mic mode, interrupt,
  state reads, config — used by voco-cli subcommands and the UI.

---

## 3. Vocabulary

- **Utterance** — one user speech segment, from VAD entry to committed final
  transcript (possibly merged across a reopen, §5.2).
- **Turn** — one utterance plus everything the system does in response.
  Every dispatched turn gets a **`turn_id`** minted at dispatch; it threads
  bridge payloads and WS events end-to-end (§8.1, §10).
- **Session** — one attached agent. Has derived identity, an auto-assigned
  **call name** (§8.3), capabilities, state, and say/queued/digest history.
- **Active session** — the single session that receives dispatched
  transcripts. Exactly one or none.
- **Local tier** — everything that can speak without the agent: cached-PCM
  earcons/acks, the phrase table, the Gemma contract.
- **Dispatch** — delivering a transcript (with `turn_id`) to the active
  session: unblocks its parked `listen`, or queues if it's working.
- **Duplex policy** — whether the mic stays armed during TTS playback (§4.4).

---

## 4. Audio pipeline

### 4.1 Capture & VAD

- Capture: PortAudio via `sounddevice`, 16kHz mono PCM frames.
- VAD: **Silero VAD** (ONNX), speech-to-speech tuned profile as defaults
  (verified against the repo):

  | Constant | Default | Meaning |
  |---|---|---|
  | `min_silence_ms` | 64 | silence that closes a segment |
  | `min_speech_ms` | 384 | speech required to open a turn / accept barge-in |
  | `min_speech_continuation_ms` | 192 | lower sustain bar while a turn is reopenable |
  | `reopen_window_ms` | 1200 | resumed speech merges into the same utterance — **only until dispatch** (§5.2) |
  | `dispatch_hold_ms` | 800 | minimum wait before irreversible dispatch (§5.2) |

- Config validation warns when `reopen_window_ms > dispatch_hold_ms` with a
  note: past the hold, reopen applies only to non-dispatched (local-reply)
  turns.
- All timing values live in config; none are hardwired (injected clock).

### 4.2 STT

STT is a port with providers (all local):

| Provider | Platform | Notes |
|---|---|---|
| `parakeet` | Windows/3090 (default), Linux | NVIDIA Parakeet TDT; fastest; v3 adds multilingual. Prefer an ONNX/sherpa-onnx runtime over full NeMo if quality holds (build-time evaluation). |
| `faster-whisper` | all | CTranslate2; portable GPU/CPU fallback; large-v3-turbo on 3090, small on CPU. |
| `whisper-mlx` / `parakeet-mlx` | macOS | Apple Silicon path for the M1. |

Contract: providers may emit partials (protocol carries them from day one —
`stt.partial` events feed captions and future prewarming); the pipeline only
*requires* finals. Empty finals are dropped.

### 4.3 TTS

One provider interface: **any OpenAI-compatible `/v1/audio/speech` endpoint,
streaming PCM**:

| Endpoint | Platform | Role |
|---|---|---|
| faster-qwen3-tts | Windows/3090 | premium voice; voice cloning later. TTFA target ~250–400ms (measure; published numbers are 4090) |
| mlx-audio (Kokoro) | M1 Mac | existing setup, port 8000 |
| kokoro-onnx (bundled runner) | any/CPU | portability floor |

Playback: single output stream, small ring buffer, ~150ms start threshold,
hard-stop flush on barge-in.

**Cached-PCM phrase bank.** At daemon start (and on voice-config change),
pre-synthesize acks/fillers/earcons through the *current* TTS voice; cache
PCM to disk keyed by `(provider, voice, phrase)`. Playable in ≤120ms, no
model. Fillers are droppable: if real speech is ready, stale fillers are
discarded, never queued behind.

### 4.4 Duplex policy & PTT

Open mic is the default experience; PTT is a first-class override.

- **`full_duplex`** (headset, or v2 AEC): VAD stays armed during playback;
  user speech ≥ `min_speech_ms` triggers barge-in (flush playback, open
  turn).
- **`half_duplex`** (speakers, no AEC): VAD is suppressed while TTS plays
  and for a ~250ms grace after; **PTT remains live and is the barge-in
  path**. Consequences the design owns (review finding): in half_duplex the
  daemon is deaf during its own playback, so voice-cancel of a pending
  dispatch and phrase-table "stop" are **unavailable on speakers in v1** —
  PTT is the override; and playback is gated while the user is talking
  (arbitration rule 0, §5.4) so a mid-utterance agent `say` can never
  suppress VAD and truncate the user's turn.
- Chirp timing per mode: full_duplex → at VAD close (instant feedback;
  cached earcons ≤400ms are exempt from rule 0); half_duplex → at dispatch/
  local-reply time (so the deaf window isn't spent on our own chirp).
- **PTT semantics** (global hotkey, default **F9**, hold-to-talk,
  configurable):
  - press: kill all playback, force-open capture (bypasses VAD entry gate);
    pressed during HOLDING → reopen/merge into the held utterance;
  - release: force end-of-utterance; skips the hold; dispatch still waits
    for the route decision (§5.2).
  - Mechanism per OS (named so the builder doesn't stall): Windows —
    low-level keyboard hook (`pynput`/`keyboard`, WH_KEYBOARD_LL under the
    hood; `RegisterHotKey` alone has no key-up); macOS — `pynput`
    CGEventTap, requires Input Monitoring permission granted to the process
    hosting the daemon (document the grant flow in README); Linux X11 —
    `pynput` grab; **Wayland — global PTT unsupported in v1**; fallback is
    `voco-cli ptt` (hold Enter in a terminal) — documented, not hidden.
- Mode is config + runtime-switchable (`voco mic full|half|muted`, WS
  command).
- Echo note: the reference project's web demo leans on browser/WebRTC AEC;
  native capture has no free AEC. v1 = headset for full_duplex, speakers for
  half_duplex; AEC (webrtc-audio-processing) is a v2 capture provider.

### 4.5 Attention modes & the wake word

Orthogonal to duplex: **when is the turn machine armed at all?**

- `always` — any speech ≥ `min_speech_ms` opens a turn (M0 default; right
  for a solo desk with headset).
- `wake` — the turn machine arms only after the wake word ("**voco**");
  it stays armed for a conversation window (default 30s, refreshed by each
  turn) so follow-ups don't need re-waking. Right for open-plan/speaker
  setups where stray speech must not become dispatched turns.
- `ptt_only` — VAD never opens turns; only PTT does.
- `muted` — nothing opens turns.

Wake detection is a port in front of VAD: **openWakeWord** (ONNX,
cross-platform, low CPU) as the default provider, ships M3. A custom
"voco" model is trained from synthetic TTS data (openWakeWord's documented
pipeline); honest caveat: two-syllable wake words run higher false-accept
rates — if bare "voco" proves noisy, the fallback is "hey voco", decided by
measurement, not taste. PTT works in every attention mode; the phrase table
and Gemma are downstream and unaffected.

---

## 5. Turn state machine

One explicit union-typed state (vocodeck rule: no scattered booleans):

```
IDLE ──speech≥min_speech──► CAPTURING ──silence≥min_silence──► HOLDING
 ▲                              ▲                                │
 │                              │ speech (or PTT press) within   │
 │                              │ reopen_window: merge,          │
 │                              └── re-transcribe ───────────────┤
 │                                                               │
 └──── turn complete ◄── ROUTING ◄── hold expiry / PTT release ──┘
                            │
              ┌─────────────┼──────────────┐
              ▼             ▼              ▼
        LOCAL_REPLY    ACK+DISPATCH     DISPATCH
        (Gemma speaks) (default path)  (forward only)

DISPATCH is a one-way door: it closes the reopen window for that turn.
Speech after dispatch = a NEW utterance (delivered live if the session is
parked again, else queued per §5.3). LOCAL_REPLY turns stay cancellable for
the full reopen_window (Gemma speech is reversible).
```

### 5.1 The instant feedback ladder (full_duplex targets, 3090 box)

| t after you stop talking | Sound | Source |
|---|---|---|
| ~64ms + ≤120ms | acknowledgment chirp | cached PCM, no model |
| ~150–400ms | (STT final lands; phrase table runs at ~0ms) | — |
| ~600–1100ms | Gemma's voice: ack or local answer | E4B + streaming TTS |
| max(hold, route) ≈ 800–1200ms | transcript dispatched to agent | — |
| agent-dependent | agent `say` speech (preempts Gemma mid-sentence) | agent + TTS |

These are **targets to validate on the 3090**, not guarantees; the TTS TTFA
term is 4090-derived (§4.3). Half_duplex shifts the chirp to dispatch time
(§4.4).

### 5.2 Speculation, the hold, and dispatch timing

Principle 7 applied:

- **Reversible immediately**: chirp at VAD close (full_duplex); STT
  finalization at VAD close; Gemma starts when the final lands; Gemma's TTS
  may start streaming — all cancellable if the user resumes speaking inside
  `reopen_window_ms` (cancel generation, flush playback, merge audio,
  re-transcribe — speech-to-speech PR-307 semantics).
- **Irreversible after the hold**: `dispatch_time = max(dispatch_hold_ms,
  route_decision_time)`. The route decision is STT-final + Gemma latency
  (Gemma timeout 800ms → forced `forward`), so worst case dispatch lands
  ~1.2–1.6s after VAD close — stated honestly, not hidden. PTT release
  skips the *hold* term but still awaits the route decision (bounded by the
  same timeout). With Gemma disabled, route decision is the phrase table
  (~0ms) and dispatch fires at hold expiry (or instantly on PTT release).
- **Dispatch closes the turn** (review blocker fix): after dispatch, resumed
  speech never merges; it opens a new turn. If the agent is now `working`,
  that new turn's forward-routed transcript is queued (§5.3).

### 5.3 While the agent works

The agent is not parked in `listen` while working, so mid-work user speech
cannot reach it through the bridge. Behavior:

- Phrase-table hard commands still work in full_duplex (daemon-local):
  `stop`/`cancel`, `mute`, `switch to <name>`. In half_duplex during
  playback, PTT is the path (§4.4).
- Everything else is **queued per-session** and delivered with the next
  `listen` return (`queued: [{ts, turn_id, text}, …]`). Queues are
  in-memory in v1: they survive agent slowness, **not daemon restarts**
  (§8.4; disk persistence is v2).
- True mid-turn interruption (Escape, `/stop`) requires harness-specific
  injection — the `inject` capability, **v2**. In v1 the daemon says so
  honestly: "Helena's mid-task — I'll pass that along when she checks in."
  (Gemma phrasing; loop-status domain, allowed.)

### 5.4 Playback arbitration

Single prioritized TTS queue. Rules, in order:

0. **No playback starts while turn state is CAPTURING or HOLDING** — queue
   it. Exception: cached earcons ≤400ms in full_duplex only.
1. User speech/PTT (barge-in) flushes everything.
2. Active-session agent `say` for the **current turn** (matched by
   `turn_id`, §8.1) preempts Gemma/local speech mid-sentence. Says for
   older turns queue behind current-turn speech instead of preempting.
3. Gemma never speaks after an agent `say` for the same `turn_id` has
   played.
4. Cached fillers are dropped (not played) if anything real is ready.
5. Background-session says are not spoken: soft per-session chime + WS
   event + digest entry (§8.3).
6. **No active session**: a forward-routed utterance plays the line-dead
   earcon + a local phrase ("no session connected") and the transcript is
   discarded — spoken about honestly, never silently dropped. When an
   active session detaches while others remain, no auto-election: the
   daemon announces and waits for a switch (auto-activate only when exactly
   one session is attached).

---

## 6. Local tier 1: the phrase table

A deterministic exact/fuzzy matcher that runs before any model, ~0ms:

- `stop` / `cancel` / `never mind` → flush playback; cancel pending
  dispatch (if HOLDING/ROUTING); queue a stop note if the agent is working.
- `mute` / `be quiet` → mic policy change.
- `switch to <name>` / `talk to <name>` → registry switch (fuzzy match on
  call names, §8.3).
- `what sessions are connected` → answered locally (registry read).

Targeted forwarding ("tell Helena …") is **not** in the phrase table:
reliably parsing addressee-plus-payload is language understanding, and the
lesson of the v1 behavior router is not to fake that with matching. In
degraded (no-Gemma) mode, forwarding goes to the active session only; a
deterministic `tell <name>` prefix matcher is parked in v2 as an experiment
that must prove itself before degraded mode promises it.

This table is the safety-critical control path and the **no-LLM degraded
mode**: with Gemma disabled or down, voco-d still works — phrase table +
ack chirp + forward-everything-verbatim.

---

## 7. Local tier 2: the Gemma contract (the first mate)

Model: **Gemma 4 E4B** served by llama.cpp `llama-server` (or Ollama/LM
Studio — any OpenAI-compatible chat endpoint; config decides). Runs on the
3090 next to TTS/STT; fits in 24GB alongside them.

The persona: the first mate / comms officer. Knows the roster, operates the
switchboard, relays, reports what was said — never does the crew's work,
never speaks for them, only quotes them. This is where "VOCO" and future
personality presets live. One persona, two brains behind it; the user never
needs to know which is talking, because they can never disagree.

### 7.1 The partition of authority

Gemma may speak ONLY about:

1. **The loop itself** — receipt, routing, session/mic state: "sent that to
   Helena", "Helena's been working four minutes on the vocodeck repo,
   branch mis-142".
2. **Attributed log content** — quoting agent `say` lines with attribution
   and age: "Helena said two tests failed, about a minute ago."
   Attribution makes contradiction impossible: it's a claim about the log,
   not the world.
3. **Social/persona content** with zero claims about the work.

Gemma must NEVER: assert work facts in its own voice, predict outcomes,
promise results ("on it — refactoring the router" is banned; "sending that
over" is the ceiling), paraphrase a command before forwarding, or answer a
work question itself.

### 7.2 Actions — the closed verb set

Gemma may emit any action **the user could already perform with one
deterministic command** — nothing else:

`switch_session(target)` · `mute` · `unmute` · `mic_mode(mode)` ·
`read_digest(session)`

The daemon validates (target exists, mode legal) and executes; invalid
actions are dropped and the speech still plays. The phrase table remains
the zero-latency deterministic path for exact utterances; Gemma covers the
fuzzy long tail ("put me through to Helena"). All verbs are loop-domain —
the authority partition is untouched. **Explicitly not in the set**:
sending input to agents (only `route: forward` does that, verbatim),
lifecycle (kill/spawn), config writes.

**Targeted forwarding** ("tell Helena to run the tests"): `forward` carries
an optional `target` (call name); default is the active session. The
forwarded text is always the **verbatim full utterance** — Gemma names the
destination, never extracts or rephrases the payload (frontier agents parse
"tell Helena to X" addressed to them without help). No implicit switch; the
active session is unchanged; Gemma's ack is routing-domain: "passed that to
Helena." If the target is working, it queues like any input. Targeted
forwarding is **Gemma-tier functionality**: with Gemma off, forwarding goes
to the active session only (§6).

### 7.3 Mechanics

- Input: system prompt (contract + persona) + a **grounding block** built
  per call: session roster (call name, repo, branch, worktree, state, age),
  active session, mic state, last N `say` lines per session with ages,
  digest counts, the final transcript.
- Output: strict JSON — `{"route": "answer" | "forward" | "ack_forward",
  "speech": "...", "action": null | {…}}`. Malformed / empty-speech-answer
  → coerced to `forward`. Timeout 800ms → `forward` + canned ack.
- Bias: prompt and coercion rules both push toward `ack_forward`. Misroutes
  fail soft in both directions; "ask Claude: …" is the verbatim escape
  hatch.
- The **daemon state machine enforces arbitration** (§5.4); the prompt is
  guidance, the code is law.
- Dispatch waits for the route decision (§5.2 timing math).

---

## 8. The bridge (agents ↔ daemon)

### 8.1 Protocol — three verbs and a register

HTTP on `localhost:7777` (config), **never bound to 0.0.0.0**. Remote access
exclusively via SSH tunnels.

- `POST /v1/bridge/register`
  `{host, user, cwd, repo?, branch?, worktree?, harness, pid, capabilities}`
  → `{session_id, call_name, display_name}`. Called implicitly by adapters
  on first use; adapters derive identity from env/process facts (CLAUDECODE
  env, codex env, `$PWD`, hostname) and git facts from one `git rev-parse`
  pass — derive-don't-ask. **`session_id` is a 128-bit random capability
  token** (unguessable; knowing it = owning the session — see §9.1).
  Adapters auto re-register on unknown-session errors (daemon restarts).
  Git facts refresh on each listen re-arm (cheap).
- `POST /v1/bridge/say` `{session_id, text, turn_id?}` → `{ok}`. Spoken per
  §5.4. `turn_id` absent → attributed to the session's most recent
  dispatched turn.
- `POST /v1/bridge/screen` `{session_id, markdown, title?, mode:
  "show"|"append"}` → `{ok}`. The rich-output channel (vocodeck v1's
  `source.screen`, carried forward so the absorbed UI's main panel is
  feedable). v1 daemon stores per-session screen content and emits
  `screen.updated`; rendering is the UI's job. voco-cli can dump it
  (`voco screen <name>`).
- `GET /v1/bridge/listen?session_id=…` — long-poll, returns within
  `listen_slice_ms` (default 50s, below harness tool timeouts):
  - `{status: "transcript", turn_id, text, queued: [...]}` — exactly-once
    delivery per transcript;
  - `{status: "rearm"}` — nothing yet; call again;
  - `{status: "detach"}` — daemon shutting down; stop listening.
  **Newest-poll-wins**: a new `listen` for a session immediately completes
  any parked one with `rearm` (Claude Code subagents share the parent's MCP
  server; concurrent polls are expected, not an error).

### 8.2 Session states

`parked` — a listen poll is outstanding. `working` — a dispatched turn is
unanswered and no poll is parked (a coding agent's normal multi-minute
state; **never times out into stale**). `idle` — registered, no poll, no
outstanding turn (say-only scripts live here; also an agent that forgot to
rearm). Post-register initial state: `idle` until the first listen.

Display flag `stale` = `idle` for > `stale_after` (default 10min): shown
dimmed, still switchable, input still queues. Dispatch to a non-parked
active session queues the transcript; if that session is `idle` (not
`working`), the line-dead earcon also plays — your words went to the queue,
not the void, and you know it.

### 8.3 Call names, switching, digests

- Every session gets a **call name** auto-assigned from a curated,
  phonetically distinct pool (STT-robust: "Helena", "Marcus", "Iris", …),
  stable per identity (hash of host+cwd+harness), shown as
  "Helena (workspace:vocodeck2)". Names are the voice grammar: "switch to
  Helena", "what did Marcus say?"
- Exactly one active session. Switch via voice (phrase table or Gemma
  action), `voco switch`, WS command, or auto-activate when the only
  session attaches. On daemon restart: no active session until the user
  switches (or exactly one re-attaches); the daemon announces readiness.
- Background says accumulate per-session digests; "what did Helena say?" is
  answered by Gemma from the log (attributed). Switching surfaces the
  unread digest count via event; digests are in-memory v1 (persistence v2).

### 8.4 Adapters & the rearm gap (top failure mode)

- **voco-cli** (universal floor — anything with a shell): `voco say "…"`,
  `voco listen`, plus operator commands: `voco
  status|sessions|switch|mic|screen|ptt|attach-cmd|new`.
- **voco-mcp** (stdio MCP, thin over the same HTTP): tools `voice_say`,
  `voice_listen`, `voice_screen`. Preferred harness integration (no
  per-call permission prompts, no shell quoting). Works in Claude Code,
  Codex, opencode (all verified MCP-capable); the CLI covers pi and
  everything else.
- **Fail-soft + self-healing** (review fix): on connection error, adapters
  retry with backoff *within the slice* and, for `listen`, synthesize
  `{status: "rearm"}` — so the agent's instructed loop keeps it parked
  through daemon restarts and tunnel blips, and the loop self-heals when
  the daemon returns. Only after sustained failure (default 10min of
  consecutive misses) does `listen` return a soft "voice daemon
  unreachable" notice. `say`/`screen` errors always soft-return one line.
- The **agent-side discipline** (paste block for CLAUDE.md /
  AGENTS.md-equivalents):

  > You are connected to a voice daemon. Call `voice_say` with 1–3 short
  > plain sentences for anything the user should hear — no markdown, paths,
  > or code in speech. Put anything substantial (lists, diffs, code, plans)
  > on the screen with `voice_screen`, then say a one-line summary. Speak
  > brief progress updates during long work. When your turn's work is
  > complete, END by calling `voice_listen` and acting on what it returns.
  > If it returns "rearm" or an unavailable notice, call it again. Treat
  > returned transcripts as the user's next instruction.

- Remaining mitigation ladder for agents that stop listening anyway:
  the §8.2 line-dead earcon (audible, lossless via queue); managed-session
  re-nudge via tmux send-keys (v2, `inject`).

---

## 9. Remote & managed sessions

### 9.1 Remote (the VS Code Remote model)

The far side gets only the thin adapter; the tunnel makes remote identical
to local:

```
# ~/.ssh/config on the machine with your mic/speakers
Host workspace
  RemoteForward 7777 localhost:7777
```

The remote voco-cli/voco-mcp talks to `localhost:7777` *on the remote box*,
which is the tunnel. Same binary, same config, text-only over SSH.
`voco attach-cmd [--host workspace]` prints the paste-ready MCP config or
CLI snippet.

**Shared-host caveat** (review fix): on a multi-user remote box, *every*
local account can reach the forwarded loopback port. Defenses: session_ids
are unguessable capability tokens (§8.1), and `attach-cmd` for a remote
host **mints and embeds a bearer token by default** (`Authorization:
Bearer`; off by default for pure-local use). Registering spoofed sessions
from a hostile shared host is still possible with the token off — hence the
default-on for remote snippets.

### 9.2 Managed sessions (attach inside-out — decision 006 carried forward)

Attach is the architecture; managed is a convenience:

- `voco new claude [--host workspace]` = (ssh +) spawn the harness **inside
  tmux** with attach pre-wired. The daemon owns the pane (kill/restart/
  list); the user owns the terminal (`tmux attach` = a normal session).
  Externally-started sessions remain first-class.
- **Windows asymmetry (decided, not deferred — review fix):** native
  Windows has no tmux; native-Windows harnesses are attach-only in v1. For
  WSL2-hosted harnesses reaching the native-Windows daemon, default-NAT
  WSL2 **cannot** reach a loopback-only Windows bind. Supported paths, in
  order: (1) **WSL2 mirrored networking** (Win11 `.wslconfig`
  `networkingMode=mirrored` — localhost becomes shared; recommended);
  (2) sshd inside WSL2 + `RemoteForward`, treating WSL2 exactly like any
  remote host (§9.1). Never `netsh portproxy` / wider binds — the
  loopback-only invariant holds.

---

## 10. WS event protocol (the durable asset)

Everything observable is an event; the future VocoDeck UI is "write a
client", not surgery. JSON envelope: `{v: 1, seq, ts, type, payload}`.
Additive evolution; consumers ignore unknown fields/types.

**Connection lifecycle** (review fix): on connect the daemon sends one
`snapshot` event (full registry incl. call names/states/capabilities,
active session, mic/duplex state, current turn state, digest counts,
current screen titles). `seq` is global and monotonic per daemon run; there
is **no replay** — reconnect ⇒ new snapshot, gap detection via `seq` is
informational only.

Events (daemon → clients): `snapshot`,
`session.attached|detached|renamed|state`, `session.activated`,
`stt.partial|final`, `turn.state`, `route.decision`,
`speech.started|interrupted|finished` (source: agent|gemma|ack),
`agent.say`, `screen.updated`, `input.queued`, `digest.updated`,
`mic.state`, `daemon.error`. All turn-scoped events (`turn.state`,
`route.decision`, `speech.*`, `agent.say`, `input.queued`) carry `turn_id`.

Commands (client → daemon, over WS or `/v1/control`): envelope
`{id, cmd, payload}` → reply `{id, ok: true, payload} | {id, ok: false,
error}`. Commands: `switch_session`, `interrupt`, `mic.set`,
`say_as_user` (typed-input path — the UI's text box later), `state.get`,
`config.get|set`.

The vocabulary lives in `src/voco/protocol/` as dataclasses + hand-rolled
validators (vocodeck house style: dependency-light, no schema libraries),
with a generated `PROTOCOL.md` so non-Python clients implement from the
doc, not the code.

---

## 11. Repo layout & engineering standards

```
vocodeck2/
  SPEC.md                     # this document
  README.md                   # quickstart per platform (incl. macOS Input
                              #   Monitoring grant, WSL2 mirrored mode)
  pyproject.toml              # uv-managed; console scripts below
  src/voco/
    protocol/                 # message vocabulary + validators (zero deps)
    core/                     # transport-free: turn machine, arbitration,
                              #   registry, contract enforcement, phrase
                              #   table, name pool
    audio/                    # capture, playback, VAD, PTT (ports + impls)
    providers/                # stt/, tts/, llm/ adapters (HTTP clients)
    bridge/                   # HTTP endpoints (thin over core)
    ws/                       # event server (thin over core)
    daemon.py                 # composition root (voco-d entry)
  adapters/
    voco_cli/                 # console script: voco-cli (alias: voco)
    voco_mcp/                 # stdio MCP server: voco-mcp
  configs/                    # windows-3090.toml, mac-m1.toml, cpu.toml
  tests/                      # pytest; fake clocks/providers; race tests
```

Console scripts: `voco-d` (daemon), `voco-cli` + alias `voco` (CLI),
`voco-mcp` (bridge). Python package: `voco`.

Standards carried from vocodeck1: hexagonal (nothing in `core/` imports
sockets, audio libs, or HTTP); impure edges injected via deps objects with
production defaults (fake clock makes hold/reopen race tests writable);
explicit union-typed state machines; errors routed to the event stream,
never swallowed (fail-silent spots are named in this spec and commented);
additive diffs; no `Any`-casting around type fights.

Config: TOML at `~/.config/voco/config.toml`; `configs/` ships per-machine
profiles (Windows/3090: parakeet + faster-qwen3-tts + llama-server E4B;
M1: mlx providers + mlx-audio; floor: faster-whisper-cpu + kokoro-onnx +
Gemma off).

### On "Python, but maybe a product"

The durable assets are the **protocol** (§10) and the **contracts**
(§5–§8) — all language-neutral. The daemon core is a few thousand lines
behind those contracts; if productization demands it, it rewrites into
Rust/Go without breaking a single client, adapter, or config. Near-term
distribution is `uvx voco-d`; a future desktop bundle ships the daemon as a
sidecar service — a solved pattern. Accepted risk.

---

## 12. Milestones

- **M0 — the loop** (prove the feel): daemon with open-mic VAD + PTT
  (Windows hook first), half/full duplex incl. rule 0, one session,
  `say`/`listen` over HTTP + voco-cli, chirp + cached acks, streaming TTS
  with barge-in, phrase table (stop/mute), turn machine with
  dispatch-closes-turn semantics. Windows/3090 providers. *Exit: hands-free
  conversation with one local Claude Code session; latency ladder measured
  and recorded against §5.1.*
- **M1 — the contract**: Gemma tier (grounding block, JSON route + action
  enum + targeted forward, coercion, timeout fallback), attributed log
  answers, call names, turn_id threading, arbitration complete, WS event
  stream + snapshot, screen verb (store + event + `voco screen`),
  `voco status/sessions`.
- **M2 — the switchboard**: multi-session registry + states (§8.2),
  switching by voice, queued-input delivery, digests + chimes, remote
  attach via SSH (docs + `attach-cmd` + bearer tokens), voco-mcp adapter,
  line-dead earcon, adapter retry/self-heal behavior.
- **M3 — comfort & reach**: managed tmux spawn (`voco new`, incl.
  `--host`), wake-word attention mode (openWakeWord provider; "voco" vs
  "hey voco" decided by measured false-accept rate), M1-Mac provider
  profile proven, kokoro-onnx floor, config polish, PROTOCOL.md generation.
- **v2 parking lot**: deterministic `tell <name>` prefix matcher for
  degraded-mode targeted forwarding (must prove reliability against real
  STT output first); AEC full-duplex on speakers; `inject` capability
  (tmux send-keys Escape/nudge, Claude channels); stream-tap adapters
  (clarp/tee narration — the *right* answer to "what's she doing right
  now": observed, not asked); `voice_status` self-pushed one-line agent
  status; ask-the-agent status ping (deliberately deferred: burns a
  frontier turn, 5–30s latency, impossible while working); disk persistence
  for queues/digests; voice cloning + personality presets; lexicon/
  normalize pass for spoken text; spoken digest summaries; UI port.

---

## 13. Risks & honest failure modes

| Risk | Exposure | Mitigation |
|---|---|---|
| Rearm gap (agent stops listening) | loop silently dead | §8.4 ladder: adapter self-heal, line-dead earcon (audible), queue (lossless in-run) |
| Daemon restart / tunnel blip while parked | loop dies permanently | adapters synthesize `rearm` + retry with backoff; loop self-heals (§8.4); queues are in-memory and lost — stated, persistence v2 |
| Echo/self-barge-in on speakers | self-interrupting loop | half_duplex default on speakers; rule 0; PTT override; AEC v2 |
| Voice-cancel unavailable on speakers | "stop" unheard during playback/hold in half_duplex | owned explicitly (§4.4); PTT is the override; AEC v2 restores it |
| Gemma misroute | wrong-but-confident local reply | authority partition caps blast radius; forward bias; coercion; closed action enum; "ask Claude:" escape hatch |
| Harness tool timeouts killing parked `listen` | error-looking noise in agent transcript | rearm slice < timeout; fail-soft result text |
| Mid-work interrupts can't reach the agent | "stop" doesn't stop v1 | honest daemon phrasing + queue; `inject` v2 |
| STT hears TTS tail / grace races | ghost utterances | half-duplex grace window; `min_speech_ms` gate; rule 0; race tests with fake clock |
| PTT on Wayland | no global hotkey | unsupported v1, documented; `voco-cli ptt` fallback |
| Shared remote host | port reachable by other accounts | capability-token session_ids; bearer token default-on for remote attach (§9.1) |
| faster-qwen3-tts on native Windows | setup friction | HTTP service — WSL2 fallback; interface unchanged |
| 3090 vs published TTFA numbers | latency ladder optimistic | numbers labeled 4090; measure at M0 exit |
| Python productization worry | packaging weight later | §11: protocol-first; core rewritable behind stable contracts |
| Where it fundamentally won't work | web-only agents, CI, no-shell/no-MCP harnesses | out of scope by design; capability matrix says so |

---

## 14. Decision log

Grilled with the user, 2026-07-03:

1. Successor project; will eventually carry the VocoDeck name and absorb
   its UI. Components voco-d / voco-cli (alias voco) / voco-mcp; dir
   `vocodeck2/`; python package `voco`.
2. Open mic (speech-to-speech-style VAD) **and** PTT, both v1; duplex
   policy handles speakers-vs-headset; AEC deferred to v2.
3. Python + uv daemon; protocol-first to keep the core rewritable.
4. Gemma tier definitely in (E4B), as **the first mate**: authority
   partition (§7.1) + closed action verb set (§7.2) + auto call names.
   voco-d degrades cleanly to phrase table + forward-verbatim without it.
5. Bridge = say/listen/screen, agent-side; switching/lifecycle is
   user-side (voice/CLI/UI). Attach inside-out (006); managed = tmux
   convenience; no wrappers ever.
6. Ask-the-agent (Gemma querying agents) rejected for v1 (frontier-turn
   cost, latency, unavailable-while-working); stream tap is the v2 answer.
7. Windows/3090 primary v1 target; M1 Mac second; CPU floor third.
8. Wake word "voco" as an attention mode (§4.5), M3 via openWakeWord;
   attention modes (`always|wake|ptt_only|muted`) are core-machine state
   from M0 even though wake detection ships later.
9. Targeted forwarding ("tell Helena …") via `forward.target` — verbatim
   payload always, no implicit switch. Gemma-tier only (user decision:
   degraded mode forwards to the active session, full stop); a
   deterministic `tell <name>` prefix matcher is a v2 experiment.
10. One same-model adversarial review (2026-07-03): 14 findings, all
   applied — dispatch-closes-turn blocker, rule 0 + half-duplex honesty,
   session-state model, adapter self-heal, turn_id threading, PTT
   mechanisms, screen channel, WS snapshot/envelope, no-active-session
   rule, dispatch timing math, 4090 labeling, capability tokens,
   newest-poll-wins, WSL2 networking decision.
