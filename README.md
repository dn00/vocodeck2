# voco (VocoDeck 2)

Local-first voice control plane for coding agents. You talk; a fully local
speech stack answers in under a second; your words reach whichever agent
session is active — Claude Code, Codex, anything with a shell or MCP —
local, in tmux, or over SSH. A browser **workbench** at the same port
shows every agent's workspace: screens, docs, annotatable diffs,
findings, chat, and live terminals. Design: [SPEC.md](SPEC.md) +
[SPEC-WORKBENCH.md](SPEC-WORKBENCH.md). Build state: [BUILD.md](BUILD.md)
+ [BUILD-WORKBENCH.md](BUILD-WORKBENCH.md).

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

The daemon serves the **workbench** at <http://127.0.0.1:7777> — the
rail groups repos → worktree workspaces → agents (live state dots);
the editor shows each workspace's pages (agent screens, pushed docs,
annotatable diffs, live terminals); the dock holds findings and chat.
A minimal protocol reference client lives at `/debug`
([PROTOCOL.md](PROTOCOL.md)).

### Review loop (workbench ⇄ agent)

```sh
voco page diff --branch          # agent publishes its branch diff
voco page doc notes.md           # ...or any markdown doc
# click a diff line in the browser to flag a finding; the agent's
# parked `voco listen` wakes with it. The agent reports back:
voco review status f-1a2b addressed --note "renamed" --commit abc123
voco review reply a-9f8e "yes — covered by test_workspace.py"
voco review export               # findings JSON + anchors sidecar
```

Diffs track the checkout live (re-push on change, per-file "changed
since rev N" chips, stale finding markers); disable per workspace with
the `workspace.live` command or globally with `[workbench]
live_git_s = 0`. MCP agents get the same verbs as tools
(`page_push`, `review_findings`, `review_reply`).

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

Managed sessions: `voco new claude --worktree feat-x --from main` spawns
the agent in a fresh sibling git worktree (killed clean → the worktree
is removed; dirty → kept). `--backend pty` gives it a daemon-owned pty
with a live interactive terminal tab in the workbench (`--backend tmux`,
the default, survives daemon restarts and supports `tmux attach`);
default via `[terminal] default_backend`. Durable review data lands in
`~/.local/share/voco/workspaces/` (`[workbench] data_dir`).

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

Foundation (M0–M3) code-complete: turn machine, arbitration, phrase
table, registry, bridge + WS, first-mate contract (calibrated against
real Gemma 4 E4B: 16/16 parse, 0 authority violations), attention modes,
tmux managed sessions + inject, AEC, debug UI, doctor. Workbench
(W0–W5) code-complete: pages + browser shell, diff review + findings
ledger + export, the unified review wake, worktrees first-class,
TerminalBackend (tmux mirror + Unix pty stream; Windows ConPTY pending
Windows validation), interdiff/staleness + live-git tracking.
291 tests, ruff + mypy + tsc clean. Live-audio validation and the
latency ladder measurement remain (need ears); live browser
click-through pending. Journals: BUILD.md, BUILD-WORKBENCH.md.
