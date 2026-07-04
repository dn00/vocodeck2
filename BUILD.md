# BUILD — plan + running journal

Read SPEC.md first; this file tracks execution state so any session can
resume. Update the journal at every checkpoint (after each commit).

## Working rules (from the user, 2026-07-03)

- No subagents; build in the main session.
- Journal progress here at each checkpoint (session-crash insurance).
- Work continuously until a milestone checkpoint; commit per coherent step.
- It's Claude's project: Claude decides build order and details within SPEC.

## M0 build order (current)

1. [x] Repo scaffold: git init, tree, pyproject (uv), .gitignore
2. [x] `protocol/` — event vocabulary + validators (pure, zero deps)
3. [x] `core/turn.py` — turn state machine (§5), injected now();
       shell contract: await next_deadline() → on_deadline()
4. [x] `core/arbitration.py` — prioritized playback queue, rules 0–5
5. [x] `core/phrases.py` — phrase table (§6)
6. [x] `core/registry.py` — sessions, parked/working/idle, queues, names
7. [x] `core/router.py` — Routed = phrase | decision; LlmTier port for M1
8. [x] `audio/` — VadGate hysteresis, CaptureBuffer pre-roll/merge,
       SpeakerPlayer (pcm + streaming), PttHotkey (pynput, optional)
9. [x] `providers/` — OpenAICompatibleTts + PhraseBank + earcons;
       SttPort (faster-whisper lazy, null)
10. [x] `bridge/http.py` — all verbs, newest-poll-wins, bearer token,
        WS events + per-connection snapshot, control endpoints
11. [x] `daemon.py` — composition root, --no-audio mode, deadline pump
12. [x] `adapters/voco_cli` — full command set, fail-soft, internal rearm
13. [x] `scripts/providers_smoke.py` — verified: VAD 0.11ms/frame on M1
14. [x] README, configs (windows-3090 / mac-m1 / cpu), 37 tests green

## M1 + M2 progress (2026-07-03, after M0)

- [x] Restructure (user feedback): core=pure / adapters=role-named edges /
      server=transport / clients=cli+mcp; Source.GEMMA → FIRST_MATE
- [x] core/first_mate.py — contract prompt, grounding, parse/coerce,
      closed action verbs, execute_action; FirstMatePort
- [x] adapters/first_mate.py — OpenAIChatFirstMate (llama-server etc.)
- [x] daemon wiring: [first_mate] config, grounding per utterance,
      immediate action execution, targeted forward
- [x] clients/voco_mcp — voice_say/voice_screen/voice_listen; verified
      live over real MCP stdio handshake against --no-audio daemon
- [ ] M1 exit: live conversation with first mate on llama-server
      (needs gemma-4-e4b GGUF + user present)
- [ ] M2 leftovers: remote-attach live test

## M3 progress (2026-07-03, buildable scope)

- [x] Static gates: ruff (lint+format) + mypy clean; CI (3 OSes)
- [x] VoiceLoop extraction (daemon slimmed; audio shell non-optional)
- [x] Attention modes (SPEC §4.5): always|wake|ptt_only|muted as core
      AttentionGate; wake WINDOW logic tested; wake DETECTOR (openWakeWord
      adapter) still pending; muted blocks PTT too (spec clarified)
- [x] tmux managed spawn: adapters/tmux.py + session.spawn|kill|panes
      control commands + voco new/kill/panes; fake-runner tests; live
      tmux test pending (tmux not installed on this Mac — validate on
      WSL2/workspace)
- [x] Control errors are clean JSON; CLI prints `error: ...`, no tracebacks
- [x] PROTOCOL.md generated (scripts/gen_protocol.py)
- [x] VoiceLoopDeps: impure edges injected with production defaults;
      wake scoring wired into the frame path (chime on wake)
- [x] adapters/wake.py: openWakeWord loader (lazy; buffers 32ms→80ms
      chunks); model download + "voco" vs "hey voco" tuning = live task
- [x] Headless end-to-end pipeline tests (test_voice_loop.py): frames→
      VAD→machine→STT→route→dispatch, muted gating, wake arming,
      full-duplex barge-in — 55 tests total
- [x] CI: PROTOCOL.md drift check
- [x] voco-tts-floor (kokoro-onnx OpenAI-compatible server, extras:
      floor) — commit f6dde91
- [x] Machine round-trip validation (scripts/roundtrip_smoke.py):
      floor TTS → real silero VAD → real faster-whisper, 3/3 exact.
      CAUGHT + FIXED a production blocker: silero v5 needs a 64-sample
      context window prepended per frame (bare frames score ~0, VAD
      would never trigger live).
- Measured on this M1 (CPU): kokoro-onnx TTFA ~400-500ms warm (no
  streaming — single chunk); faster-whisper small ~1.2s/utterance →
  Mac profile wants whisper-mlx or a smaller model at calibration.
- [ ] config.set persistence — deferred

## First-mate calibration vs REAL Gemma 4 E4B (2026-07-03, M1, llama.cpp)

Harness: scripts/mate_calibrate.py (16-utterance suite, realistic
grounding). Model: ggml-org/gemma-4-E4B-it-GGUF:Q4_K_M via llama-server
(cached in ~/.cache/huggingface, ~4.2GB — the same file the Mac profile
uses).

- Run 1 (naive): parse 3/16 — Gemma 4 THINKING TOKENS ate the completion
  budget (the exact failure the Reddit thread warned about). Fixes:
  `--reasoning-budget 0` server flag + response_format json_object
  (llama.cpp grammar-enforces it; adapter now sends it, config
  `json_mode`).
- Run 2: parse 16/16, route 14/16. Prompt tightened: code/file questions
  are ALWAYS work questions; asking-about-a-session ≠ switch action.
- Run 3 (final): parse 16/16, route 15/16, targets 2/2, actions 2/2,
  ZERO authority violations. Residual: pure deck commands sometimes route
  forward alongside the (correct) action — benign stray message; phrase
  table catches canonical switch phrasings first anyway.
- Latency M1 Metal: p50 ~2.0s (generation-bound) → over the 800ms router
  timeout; on Mac the mate coerces to forward (fast dispatch, no local
  answers) unless timeout_ms is raised (now config) or a smaller model is
  used. 3090 expected well under budget — re-run harness there.
- Router timeout now configurable: [first_mate] timeout_ms.

## v2 pulled forward (2026-07-03, user-directed)

- [x] Inject capability (tmux send-keys, universal — NOT Claude-only):
      adapters derive TMUX_PANE (derive-don't-ask) → "inject" capability;
      voice "stop"/interrupt sends Escape to the active session's pane;
      queued-idle dispatch schedules a 2s nudge typed into the composer
      (self-healing rearm). Fail-silent with daemon.error. LIVE-VALIDATED:
      tmux 3.7b installed; spawn→send_text→capture_pane→kill round trip
      green (test_inject_live.py).
- [x] AEC: pure-numpy partitioned-block FDAF (core/echo.py) — webrtc/
      speex bindings don't build on arm64, and zero-dep is more portable
      anyway. 8 partitions ≈256ms tail absorbs device latency. Synthetic
      room validation: ERLE >12dB after convergence, bit-exact passthrough
      without playback, near-end survives (corr >0.7). Wired: speaker
      device-callback tap → resample 24k→16k → reference; mic frames pass
      through canceller before capture/VAD. Config: [audio] aec = true
      (default OFF until live-tuned). No double-talk detector yet —
      half_duplex stays the speakers fallback until live validation.
- Decisions from idea review: simple localhost UI = recommended next
  (protocol is ready for it); own tmux/wrapper = NO for stdio WRAPPERS
  (006 re-adopted by choice; the reasons transfer) — a pty HOST (tmux
  today, a ConPTY host for native Windows later) was never banned;
  terminal mirroring = capture-pane peek once UI exists.
- Known limitation (wake mode): if the wake word and the command share
  one breath with no pause, the VAD speech-run started before arming and
  speech_started won't refire until a silence gap — "voco, <pause>, do X"
  works; same-breath needs a VAD reset-on-wake (v2 refinement).

## Overnight stretch (2026-07-03 night, user asleep — "solid state" goal)

User directives for this stretch: backend/daemon/cli = production grade;
core UX (mate↔backend integration, voice) matters even more; simple web
UI is fine; subagents allowed for REVIEW/next-steps only, never building;
repo pushed to GitHub as PRIVATE (github.com/dn00/vocodeck2); commit often.

- [x] session.detach (unparks the agent's listen with status=detach) +
      session.peek (terminal mirror by call name or raw tmux target) +
      voco detach/peek; graceful SIGTERM/SIGINT (verified live: kill →
      "shut down cleanly").
- [x] Async control surface: on_control awaitable; tmux/ssh subprocess
      commands run in the executor — a slow ssh can no longer stall WS
      delivery/listen polls/speech.
- [x] Debug UI at / and /ui: self-contained page (no CDN), one WS,
      sessions/states/badges, screen (markdown-lite), terminal peek tab
      with auto-refresh, live event log (stt.partial filtered), mic +
      attention controls, interrupt, type-as-user, detach, token bar
      (?token= WS auth for browsers). Snapshot enriched: queued count,
      screen_markdown, say_tail(10), last_seen; screen.updated carries
      full markdown.
- [x] BUG (live-smoke find): bridge register dropped tmux_pane/
      tmux_session/host_alias → inject capability never activated over
      real HTTP. Fixed + regression test.
- [x] Misroute guard: mate configured but times out → 'tell/ask <known
      name>' keeps its destination (registry facts + conservative fuzz);
      degraded mode still never targets (§14.9 upheld). Grounding gains
      queued_inputs per session.
- [x] daemon.error → stderr with timestamp; clean one-line exits for bad
      --config and port-in-use.
- [x] voco doctor: daemon/tts/mate/tmux/extras diagnostic. TTS probe
      synthesizes a REAL test phrase and requires audio bytes (OrbStack
      squats :8880 and answers 200 with 0 bytes — a reachability check
      lies). Verified live against a real kokoro behind OrbStack.
- [x] Pane-state heuristics (herdr-inspired, clean-room — it's AGPL):
      core/pane_state.py classifies peeked panes waiting/working/shell,
      None when unsure; session.peek returns {text, hint}; UI shows the
      chip. Groundwork for proactive "Marcus is waiting on you" lines.
- 79 tests, ruff+mypy+format clean, PROTOCOL.md regenerated (12 commands).

## Overnight stretch, second half (2026-07-04 pre-dawn)

User green-lit (before going afk): pane watcher, durable sessions,
config validation, mate streaming; SQLite allowed "only if it fits"
(it didn't — JSON snapshot at this scale); subagents fully off again
("for now"; one review + one next-steps agent ran under the earlier
window — findings below); repo private on GitHub, push per slice.

- [x] Config schema validation + config.set (voco/config.py): hand-rolled
      schema, errors refuse boot together, unknown keys warn; base +
      config.local.toml overrides merge — the base file is NEVER
      rewritten; hot-apply whitelist (duplex/attention/timeout_ms via
      Router.set_timeout), everything else honestly restart_required;
      voco config get/set.
- [x] Durable sessions: Registry.dump/restore (versioned, defensive) +
      adapters/state_store.py (atomic, 0600 — tokens inside; corrupt →
      .corrupt sidecar + fresh boot); daemon restores on run, debounced
      saver on bus events, final save on shutdown. Queued words + tokens
      survive restarts; CLI caches stay valid (no 410 churn).
- [x] ADVERSARIAL REVIEW (subagent, 9 findings) — all fixed same night:
      stored XSS via screen-markdown link href (esc() now escapes quotes;
      URL class forbids them) — MAJOR; bare numbered list read as
      'waiting' (now needs ask-context) — MAJOR; fallback fuzz 0.75→0.8
      (Noah≠Nova); doctor default port mismatch + contradictory FAIL row;
      WS commands off the receive loop (reply via the pump queue — single
      writer); TmuxManager built on loop thread; UI pending-map rejected
      on reconnect; screen.updated double-render.
- [x] Pane watcher (watcher.py): polls inject-capable non-parked panes
      (3s), pane.hint event + snapshot field + UI badge + grounding
      terminal=<hint>; 'waiting' needs two consecutive sightings, one
      announcement per episode → "X looks like they're waiting on you."
- [x] Mate speech streaming ([first_mate] stream, default OFF):
      SpeechStream extracts the speech field from the streaming JSON;
      MateSpeechChannel sentence-cuts into one playback item; decision
      parsing byte-identical to the plain path. Machine-validated vs real
      Gemma 4 E4B: warm first speech delta 0.75-1.6s vs 1.8s+ plain
      (scripts/mate_stream_smoke.py); found + fixed SSE keep-alive pool
      poisoning (plain-after-stream died); adapter socket budget now
      derives from timeout_ms; retry-once on stale connections.
- References mined (user links): faster-qwen3-tts = our OpenAI TTS
  contract, 156ms TTFA on 4090-class, NO incremental text input →
  sentence-chunked TTS is the right mate-streaming design (built);
  speech-to-speech = we already mirror its VAD/speculation constants;
  its live-transcription deltas map to our stt.partial (future).
- 107 tests; ruff+mypy+format clean; PROTOCOL.md 20 events/12 commands.

### v-next (updated)

- Flip [first_mate] stream default ON after ears validation; then wire
  streamed ack_forward turn_id attribution (streamed item currently
  carries turn_id=None — arbitration rule-3 fidelity).
- DeepFilterNet-style mic enhancement stage in front of VAD (separate
  from AEC) — VAD+STT robustness in noisy rooms.
- stt.partial production + speculative routing during HOLDING;
  wake same-breath VAD reset; multi-utterance mate memory; packaging
  (uvx/launchd/systemd) + PtyHost seam for native Windows (ranked list
  from the next-steps agent lives in the session log, 2026-07-04).
- One-command onboarding: `uv sync` should include all needed extras by
  default (or a single `--extra all`); `voco setup` should generate
  `.mcp.json` automatically; MCP may belong in base deps. Current setup
  has too many manual steps (extra-juggling, hand-authored .mcp.json).

## Live-test bugs (2026-07-03, M1 Mac, user present) — FIXED 2026-07-03 (see journal)

- [x] **Echo feedback (full_duplex + speakers):** mac-m1 profile now defaults
      half_duplex; set_duplex applies suppression MID-playback (the rescue
      move used to wait for the next playback edge); wake scorer is deaf
      during playback; pre-roll ring is dropped when suppression lifts so
      the bot's speaker tail can't open the next utterance. AEC still
      default-off pending live tuning (unchanged).
- [x] **config.set hot-apply lies:** NOT reproducible on HEAD with a live
      voice loop (regression-tested). The lie WAS real for voice=None
      (headless/degraded boot): applied:true with no runtime change —
      now honest (restart_required; mic.set 400s cleanly).
- [x] **UI dropdowns don't sync:** snapshot now carries {mic:{duplex,
      attention}} (BridgeServer snapshot_extra); dropdowns mirror daemon
      state (disabled when headless), revert on failed mic.set; interrupt
      button flashes on success.
- [x] **Typed vs spoken input not differentiated:** origin: voice|typed on
      QueuedInput (persisted), route.decision, input.queued, and listen
      payloads; CLI/MCP render `[typed]` marks.
- [x] **Queued-while-working backlog:** per-item age_s computed at delivery;
      format_transcript (shared CLI+MCP) marks stale (>60s) lines with
      age; last line stays the current instruction. Nothing dropped
      silently.
- [x] **voice_listen blocks the agent turn:** `voco listen --stream` — the
      agent backgrounds it once, transcripts arrive as flushed stdout
      lines; MCP instructions teach both modes. Verified live end-to-end.
      Plus voice_init (the original sketch): writes the exact listener
      script (0700; pins the venv interpreter + URL + token — agent
      shells lack `voco` on PATH and fish rejects VAR=x prefixes) and
      returns `bash <path>` to background. Verified over a real MCP
      stdio handshake: init → background script (clean env) → `voco
      input` → '[typed] ...' stream line.
- [x] **voco new: tmux session doesn't persist:** spawn now pins the pane
      (remain-on-exit), waits 0.8s, checks pane_dead, and raises with the
      exit status + pane tail; corpse cleaned up. Live-validated: bad
      binary → "exited (status 127) right after spawn".
- [x] **Always-on attention clips first words:** two real defects — pre-roll
      (320ms) was SMALLER than the VAD entry run (384ms), and any single
      sub-threshold frame zeroed the entry accumulator. Pre-roll now
      derives from min_speech_ms + 320ms margin; entry run only resets
      after a real (>= min_silence) gap.
- [x] **Multiple sessions collapse into one:** identity gains an `instance`
      component: TMUX_PANE > CLAUDE_CODE_SESSION_ID > None (both verified
      inherited by Bash children AND MCP servers, so CLI+MCP from one
      agent still merge). Registry keys + CLI cache filename include it.
- [ ] **PTT doesn't work from browser:** F9 keypress in the debug UI doesn't
      trigger PTT — browser can't capture global hotkeys. Needs a native
      desktop client (Rust?) for global hotkey support, system tray, and
      always-on mic access regardless of focused window. (v-next)
- [ ] **Kokoro voice quality:** kokoro-onnx on M1 is functional but voice
      quality is noticeably worse than faster-qwen3-tts / the speech-to-
      speech reference project. Explore better M1-local TTS or remote GPU
      TTS option. (v-next)

## M0 exit — REMAINING (needs user present / live audio)

- [ ] `uv sync --extra stt` + providers_smoke with mlx-audio running (Mac)
- [ ] live mic loop: speak → chirp → transcript → dispatch → agent say →
      streaming TTS; barge-in by voice (full_duplex) and PTT
- [ ] measure the §5.1 latency ladder, record numbers in SPEC
- [ ] Windows/3090: clone, uv sync, providers_smoke, same validation
- Then M1 (Gemma contract) per SPEC §12.

M0 exit (SPEC §12) needs live mic validation with the user present; the
code-complete checkpoint is: all above built, pytest green, daemon boots,
bridge round-trip works via curl/CLI with a fake-audio harness.

## Deviations / build-time decisions

- turn.py: pre-dispatch speech ALWAYS merges (dispatch is THE turn
  boundary); reopen_window_ms bounds only the REOPENABLE state. Spec-
  compatible clarification of §5.2 (noted in module docstring).
- registry: same identity re-register returns the same session record and
  token (idempotent); new listen ends the working turn (outstanding_turn
  cleared on on_listen_start).

## Live-test bugfix session (2026-07-03 evening, user present then afk)

Working rule reaffirmed by the user this session: NEVER use subagents —
everything in the main session. CodeGraph index initialized (used for
exploration). All nine buildable live-test bugs fixed, one commit per
slice, 130 tests green + ruff + mypy + PROTOCOL drift after each:

- Repro-first on the hot-apply bug paid off: HEAD was actually correct
  with a live voice loop; the reproducible lie was voice=None reporting
  applied:true. Likely live story: a degraded (headless) boot after one
  of the echo-chaos restarts + UI that could only learn mic state from
  transient events. Both halves fixed (honest headless + mic in
  snapshot) so the whole class is gone either way.
- Smoke-test gotcha worth remembering: a scratch `voco-d --no-audio`
  without a [state] dir override restores + rewrites the USER'S
  ~/.local/state/voco/registry.json (ghost active session ate my test
  dispatches; sanitized the file after). Hermetic smokes need their own
  state dir AND VOCO_CACHE.
- gitignore now excludes per-machine files: *.local.toml, .mcp.json,
  .codegraph/.

## Journal

- 2026-07-03: SPEC.md v1.1 finalized (review findings + first-mate +
  wake-word + targeted-forward corrections). Repo initialized.
- 2026-07-03 (commit b88f73a): core complete, 25 tests green.
- 2026-07-03 (commit 1f78832): audio/providers/bridge/daemon/CLI complete;
  37 tests; verified headless: voco-d --no-audio boot + CLI loop
  (register→"Wanda"→input→dispatch→listen→transcript→working state).
  Bridge race fixed: evicted poll cleanup no longer unparks newer poll.
- 2026-07-03 (this commit): smoke script (VAD 0.11ms/frame M1; stt/tts
  fails are env-only: extras not installed, mlx-audio down), configs,
  README. **M0 CODE-COMPLETE** — remaining exit items above need live
  audio with the user. User note honored: `voco listen` parks internally
  (one bash call, no rearm churn); channels + MCP extensions are just
  additional bridge clients (voco-mcp lands M2).
