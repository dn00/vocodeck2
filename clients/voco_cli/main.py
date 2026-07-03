"""voco-cli — the universal bridge adapter + operator commands (SPEC §8.4).

ROLE: `voco say|listen` for agents (anything with a shell), plus operator
commands (status/sessions/switch/mic/screen/attach-cmd). Fail-soft contract:
connection errors never raise into an agent's turn — say returns a one-line
notice, listen synthesizes rearm and keeps parking (self-healing loop).

INVARIANTS: identity is derived (hostname, cwd, git facts, env heuristics),
never asked; the session token is cached per (host, cwd, harness) in
~/.cache/voco so repeat invocations reuse the session.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_URL = os.environ.get("VOCO_URL", "http://127.0.0.1:7777")
CACHE_DIR = Path(os.environ.get("VOCO_CACHE", Path.home() / ".cache" / "voco"))
SOFT_FAIL = "voice daemon unreachable — continue without voice"
RETRY_WINDOW_S = 600  # sustained-failure ceiling for listen (SPEC §8.4)


def _git(args: list[str], cwd: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=2
        )
        return out.stdout.strip() or None if out.returncode == 0 else None
    except Exception:
        return None


def derive_identity() -> dict:
    cwd = os.getcwd()
    harness = "unknown"
    if os.environ.get("CLAUDECODE"):
        harness = "claude"
    elif os.environ.get("CODEX_SANDBOX") or os.environ.get("CODEX"):
        harness = "codex"
    repo_root = _git(["rev-parse", "--show-toplevel"], cwd)
    return {
        "host": socket.gethostname().split(".")[0],
        "user": os.environ.get("USER") or os.environ.get("USERNAME") or "?",
        "cwd": cwd,
        "repo": Path(repo_root).name if repo_root else None,
        "branch": _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd),
        "worktree": repo_root,
        "harness": harness,
        "pid": os.getpid(),
    }


def control(client: Client, cmd: str, payload: dict, timeout: float = 35.0) -> int:
    """Operator command: print the result or the server's error, no tracebacks."""
    try:
        result = client._request("POST", f"/v1/control/{cmd}", payload, timeout=timeout)
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode())
            print(f"error: {body.get('error', e)}", file=sys.stderr)
        except Exception:
            print(f"error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


class Client:
    def __init__(self, base_url: str = DEFAULT_URL, token: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token or os.environ.get("VOCO_TOKEN")

    def _request(
        self, method: str, path: str, body: dict | None = None, timeout: float = 55.0
    ) -> dict:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())

    # ---- session (cached token, re-register on 410) --------------------------

    def _cache_path(self, identity: dict) -> Path:
        key = f"{identity['host']}-{Path(identity['cwd']).name}-{identity['harness']}"
        return CACHE_DIR / f"session-{key}.json"

    def session(self) -> dict:
        identity = derive_identity()
        cache = self._cache_path(identity)
        if cache.exists():
            try:
                return json.loads(cache.read_text())
            except json.JSONDecodeError:
                pass
        return self.register(identity)

    def register(self, identity: dict | None = None) -> dict:
        identity = identity or derive_identity()
        info = self._request(
            "POST",
            "/v1/bridge/register",
            {**identity, "capabilities": ["say", "listen", "screen"]},
            timeout=5,
        )
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self._cache_path(identity).write_text(json.dumps(info))
        return info

    def _with_session(self, fn):
        """Run fn(session_id); on 410, re-register once and retry."""
        sess = self.session()
        try:
            return fn(sess["session_id"])
        except urllib.error.HTTPError as e:
            if e.code == 410:
                sess = self.register()
                return fn(sess["session_id"])
            raise

    # ---- verbs ---------------------------------------------------------------------

    def say(self, text: str) -> str:
        try:
            self._with_session(
                lambda sid: self._request(
                    "POST",
                    "/v1/bridge/say",
                    {"session_id": sid, "text": text},
                    timeout=5,
                )
            )
            return "ok"
        except Exception:
            return SOFT_FAIL

    def screen(self, markdown: str, title: str | None, mode: str) -> str:
        try:
            self._with_session(
                lambda sid: self._request(
                    "POST",
                    "/v1/bridge/screen",
                    {
                        "session_id": sid,
                        "markdown": markdown,
                        "title": title,
                        "mode": mode,
                    },
                    timeout=5,
                )
            )
            return "ok"
        except Exception:
            return SOFT_FAIL

    def listen_once(self) -> dict:
        """One slice; connection errors synthesize rearm (SPEC §8.4)."""
        try:
            return self._with_session(
                lambda sid: self._request(
                    "GET",
                    f"/v1/bridge/listen?session_id={urllib.parse.quote(sid)}",
                    timeout=65,
                )
            )
        except Exception:
            time.sleep(2)
            return {"status": "rearm", "_synthesized": True}

    def listen(self) -> dict:
        """Park until a transcript arrives; self-heals through daemon
        restarts up to RETRY_WINDOW_S of sustained failure."""
        failing_since: float | None = None
        while True:
            result = self.listen_once()
            if result.get("status") == "transcript":
                return result
            if result.get("status") == "detach":
                return {"status": "detach"}
            # rearm: distinguish daemon-alive rearm from synthesized ones
            # by probing registration cheaply every loop is overkill; the
            # sustained-failure window only advances on socket errors.
            if result.get("_synthesized"):
                failing_since = failing_since or time.monotonic()
                if time.monotonic() - failing_since > RETRY_WINDOW_S:
                    return {"status": "unavailable"}
            else:
                failing_since = None


def cmd_watch(client: Client) -> int:
    """Tail the daemon's WS event stream (snapshot first, then live)."""
    import asyncio

    import aiohttp

    async def run() -> None:
        headers = {}
        if client.token:
            headers["Authorization"] = f"Bearer {client.token}"
        ws_url = client.base_url.replace("http", "ws", 1) + "/v1/events"
        async with (
            aiohttp.ClientSession(headers=headers) as session,
            session.ws_connect(ws_url) as ws,
        ):
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                env = json.loads(msg.data)
                stamp = time.strftime("%H:%M:%S", time.localtime(env.get("ts", 0)))
                payload = json.dumps(env.get("payload", {}))
                if len(payload) > 110:
                    payload = payload[:110] + "…"
                seq, typ = env.get("seq", 0), env.get("type", "?")
                print(f"{stamp} {seq:>5} {typ:<20} {payload}")

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        return 0
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_attach(args, client: Client) -> int:
    mcp = {
        "mcpServers": {
            "voco": {"command": "voco-mcp", "env": {"VOCO_URL": client.base_url}}
        }
    }
    print("# MCP config (Claude Code: .mcp.json / Codex: config.toml equivalent):")
    print(json.dumps(mcp, indent=2))
    print('\n# CLI fallback: agents call `voco say "..."` and `voco listen`.')
    print("# Remote host: add to ~/.ssh/config -> RemoteForward 7777 localhost:7777")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="voco")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_say = sub.add_parser("say")
    p_say.add_argument("text")
    sub.add_parser("listen")
    p_screen = sub.add_parser("screen")
    p_screen.add_argument("markdown")
    p_screen.add_argument("--title", default=None)
    p_screen.add_argument("--append", action="store_true")
    sub.add_parser("status")
    sub.add_parser("sessions")
    p_switch = sub.add_parser("switch")
    p_switch.add_argument("name")
    p_mic = sub.add_parser("mic")
    p_mic.add_argument(
        "mode",
        choices=[
            "full_duplex",
            "half_duplex",
            "always",
            "wake",
            "ptt_only",
            "muted",
        ],
    )
    p_new = sub.add_parser("new")
    p_new.add_argument("harness", help="command to run, e.g. claude")
    p_new.add_argument("--name", default=None)
    p_new.add_argument("--cwd", default=None)
    p_new.add_argument("--host", default=None)
    p_kill = sub.add_parser("kill")
    p_kill.add_argument("name", help="tmux session name (voco-...)")
    p_kill.add_argument("--host", default=None)
    p_panes = sub.add_parser("panes")
    p_panes.add_argument("--host", default=None)
    sub.add_parser("watch")
    p_input = sub.add_parser("input")  # typed input path (say_as_user)
    p_input.add_argument("text")
    sub.add_parser("attach-cmd")
    args = parser.parse_args()

    client = Client()
    if args.cmd == "say":
        print(client.say(args.text))
    elif args.cmd == "listen":
        result = client.listen()
        if result.get("status") == "transcript":
            for q in result.get("queued", []):
                print(f"[queued while working] {q['text']}")
            print(result["text"])
        else:
            print(SOFT_FAIL)
            sys.exit(0)  # fail-soft: never a hard error in an agent turn
    elif args.cmd == "screen":
        print(
            client.screen(
                args.markdown, args.title, "append" if args.append else "show"
            )
        )
    elif args.cmd in ("status", "sessions"):
        try:
            state = client._request("POST", "/v1/control/state.get", {}, timeout=5)
        except Exception:
            print(SOFT_FAIL)
            sys.exit(1)
        if args.cmd == "status":
            print(json.dumps(state, indent=2))
        else:
            for s in state.get("sessions", []):
                active = "*" if s["session_id"] == state.get("active_session") else " "
                unread = s["unread_digest"]
                print(f"{active} {s['display_name']}  [{s['state']}]  unread={unread}")
    elif args.cmd == "switch":
        sys.exit(control(client, "switch_session", {"name": args.name}, timeout=5))
    elif args.cmd == "mic":
        knob = "duplex" if args.mode in ("full_duplex", "half_duplex") else "attention"
        sys.exit(control(client, "mic.set", {knob: args.mode}, timeout=5))
    elif args.cmd == "new":
        sys.exit(
            control(
                client,
                "session.spawn",
                {
                    "harness": args.harness,
                    "name": args.name,
                    "cwd": args.cwd,
                    "host": args.host,
                },
            )
        )
    elif args.cmd == "kill":
        sys.exit(
            control(client, "session.kill", {"name": args.name, "host": args.host})
        )
    elif args.cmd == "panes":
        sys.exit(control(client, "session.panes", {"host": args.host}))
    elif args.cmd == "watch":
        sys.exit(cmd_watch(client))
    elif args.cmd == "input":
        sys.exit(control(client, "say_as_user", {"text": args.text}, timeout=5))
    elif args.cmd == "attach-cmd":
        sys.exit(cmd_attach(args, client))


if __name__ == "__main__":
    main()
