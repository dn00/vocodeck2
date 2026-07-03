# voco (VocoDeck 2)

Local-first voice control plane for coding agents. You talk; a fully local
speech stack answers in under a second; your words reach whichever agent
session is active — Claude Code, Codex, anything with a shell or MCP —
local, in tmux, or over SSH. Design: [SPEC.md](SPEC.md). Build state:
[BUILD.md](BUILD.md).

## Quickstart

```sh
uv sync --extra stt --extra ptt --extra dev

# 1. Probe your providers first (downloads the silero VAD model):
uv run python scripts/providers_smoke.py --config configs/mac-m1.toml

# 2. Run the daemon:
uv run voco-d --config configs/mac-m1.toml
#    (headless bring-up without mic/speakers: add --no-audio)

# 3. Attach an agent — print the paste-ready snippet:
uv run voco attach-cmd

# Not sure what's missing? One-command environment diagnostic:
uv run voco doctor
```

The daemon also serves a debug UI at <http://127.0.0.1:7777> — sessions
with live states/digests/queues, each agent's screen, a terminal mirror
(peek) with waiting/working hints, the live event stream, mic controls,
and a type-as-user input. It speaks only the public WS protocol
([PROTOCOL.md](PROTOCOL.md)), so it doubles as the reference client.

Agent-side discipline (paste into CLAUDE.md / AGENTS.md equivalent):

> You are connected to a voice daemon. Call `voco say "..."` with 1–3 short
> plain sentences for anything the user should hear — no markdown, paths,
> or code in speech. Put anything substantial on the screen with
> `voco screen`, then say a one-line summary. Speak brief progress updates
> during long work. When your turn's work is complete, END by running
> `voco listen` and acting on what it prints. Treat printed transcripts as
> the user's next instruction.

`voco listen` parks inside one blocking call (rearm slices are handled
internally — one bash/tool call per user turn, no churn) and self-heals
through daemon restarts.

Operator commands: `voco sessions` / `switch <name>` / `mic <mode>` /
`input <text>` / `watch` (event tail) / `new <cmd>` / `kill` / `panes`
(tmux managed sessions) / `peek <name>` (terminal mirror) /
`detach <name>` / `doctor`.

## Platform profiles

| Config | Machine | STT | TTS |
|---|---|---|---|
| `configs/windows-3090.toml` | Windows + RTX 3090 (primary) | faster-whisper large-v3-turbo (CUDA) | faster-qwen3-tts |
| `configs/mac-m1.toml` | Apple Silicon | faster-whisper small | mlx-audio (Kokoro) |
| `configs/cpu.toml` | anything | faster-whisper small (CPU) | kokoro-onnx |

## Remote sessions (VS Code Remote model)

```
# ~/.ssh/config on the machine with your mic
Host workspace
  RemoteForward 7777 localhost:7777
```

On the remote box, `voco`/`voco-mcp` talk to `localhost:7777` — the tunnel.
Same binary, text only, nothing else leaves the machine. On shared hosts,
set `[bridge] token` (SPEC §9.1).

## Status

M0–M3 buildable scope code-complete: turn machine, arbitration, phrase
table, registry, bridge + WS, first-mate contract (calibrated against
real Gemma 4 E4B: 16/16 parse, 0 authority violations), attention modes,
tmux managed sessions + inject, AEC, debug UI, doctor — 79 tests, ruff +
mypy clean, CI on 3 OSes. Live-audio validation and the latency ladder
measurement are the remaining exit items (need ears). Milestones:
SPEC §12; running journal: BUILD.md.
