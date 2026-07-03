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
2. [ ] `protocol/` — event vocabulary + validators (pure, zero deps)
3. [ ] `core/clock.py` + `core/turn.py` — turn state machine (§5) with
       injected clock; fake-clock tests incl. reopen/hold/PTT races
4. [ ] `core/arbitration.py` — prioritized playback queue, rules 0–6 (§5.4)
5. [ ] `core/phrases.py` — phrase table (§6)
6. [ ] `core/registry.py` — sessions, states parked/working/idle (§8.2),
       queues, call names (pool in M1; M0 uses one session)
7. [ ] `core/router.py` — route decision port: phrase table now, Gemma M1
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

- (record here as they happen; SPEC change → update SPEC.md too)

## Journal

- 2026-07-03: SPEC.md v1.1 finalized (review findings + first-mate +
  wake-word + targeted-forward corrections). Repo initialized. Starting
  step 2 (protocol).
