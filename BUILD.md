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
- [ ] M2 leftovers: tmux managed spawn (M3), remote-attach live test

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
