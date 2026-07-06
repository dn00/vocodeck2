# HANDOFF — session of 2026-07-04 → next session

Read BUILD.md "Triage decisions" + journal first. Working rules: NO
subagents ever; commit per slice; push to origin/main; journal BUILD.md
at checkpoints; gates per commit = pytest + ruff check/format + mypy +
gen_protocol drift.

## Where things stand

All four triage-round-2 builds are DONE (168 tests green):
mate-off-critical-path, sentence-chunked TTS + mate voice, mate prompt
guardrails, and turn-layer patience (semantic endpointing — this
session's slice: looks_complete + incomplete_hold_ms, hot-apply
patience keys, UI presets, configs/SPEC/tests). Then core-loop
hardening cross-checked with a Codex review (see BUILD.md journal
2026-07-04 later): no stranded ROUTING turns, crash-proof deadline
pump, VAD suppress closes mid-speech segments, turn.patience event.

## NOTHING IN FLIGHT — working tree clean at last commit
(check `git log origin/main..main`: the hardening commit may be
unpushed — the push needed user approval)

## Backlog (task list + BUILD v-next)

- Mate hot-reload via config.set (scoped to [first_mate].* rebuild).
- Stream-stall: needs live repro on current HEAD before it's a ticket.
- T-proxy streaming text (premium; `voco run <harness>` sets
  ANTHROPIC_BASE_URL) — design only.
- Native desktop client (browser PTT), kokoro voice quality (v-next).
- M1 exit: live mate conversation validation (user present); M0 exit
  live-audio items in BUILD.md.

## Environment gotchas

- llama-server with real Gemma is (was) live on :8080 — calibration:
  `uv run python scripts/mate_calibrate.py --base-url
  http://127.0.0.1:8080/v1 --model "ggml-org/gemma-4-E4B-it-GGUF:Q4_K_M"`.
- User's daemon on :7777 + MCP servers run OLD code until restarted
  (MCP respawns with the agent's Claude session, not the daemon!).
- Hermetic smokes MUST override [state] dir AND VOCO_CACHE, else you
  restore/pollute the user's ~/.local/state/voco/registry.json.
- tmux leftovers: `voco-pytest-inject` can linger if a test run is
  interrupted; kill before re-running the suite.
