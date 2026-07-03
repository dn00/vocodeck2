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
8. [ ] `audio/` — silero VAD wrapper (hysteresis per §4.1), capture,
       playback ring buffer, PTT hotkey (optional import)
9. [ ] `providers/` — STT port (faster-whisper first), TTS client
       (OpenAI-compatible streaming PCM), phrase-bank cache
10. [ ] `bridge/http.py` — register/say/screen/listen + control + WS events
11. [ ] `daemon.py` — composition root, config load (TOML)
12. [ ] `adapters/voco_cli` — say/listen/status/sessions/mic/ptt/attach-cmd
13. [ ] `scripts/providers_smoke.py` — stand up + measure each provider
14. [ ] README quickstart; full test run; M0 checkpoint report to user

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
- 2026-07-03 (commit b88f73a): core complete, 25 tests green. Next:
  audio layer (silero VAD wrapper w/ hysteresis, capture, playback, PTT).
