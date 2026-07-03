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
  (protocol is ready for it); own tmux/wrapper = NO (decision 006);
  terminal mirroring = capture-pane peek once UI exists.
- Known limitation (wake mode): if the wake word and the command share
  one breath with no pause, the VAD speech-run started before arming and
  speech_started won't refire until a silence gap — "voco, <pause>, do X"
  works; same-breath needs a VAD reset-on-wake (v2 refinement).

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
