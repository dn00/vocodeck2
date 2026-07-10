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
import hashlib
import json
import os
import re
import secrets
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
STALE_AFTER_S = 60  # backlog older than this gets an age mark

# Terminal statuses an agent reads verbatim — each says clearly whether
# to restart the listener (a detach must never read as a crash).
MSG_DETACHED = (
    "voice session ended by the user — stop listening; do not restart the listener."
)
MSG_SHUTDOWN = "voice daemon shutting down — stop listening."
MSG_SUPERSEDED = (
    "another listener took over this session — stop this one; do not restart it."
)


def terminal_message(result: dict) -> str | None:
    """Map a non-transcript listen result to its agent-facing line."""
    status = result.get("status")
    if status == "detach":
        return MSG_DETACHED if result.get("reason") == "detached" else MSG_SHUTDOWN
    if status == "superseded":
        return MSG_SUPERSEDED
    if status == "unavailable":
        return SOFT_FAIL
    return None


def _fmt_age(age_s: int) -> str:
    if age_s < 60:
        return f"{age_s}s ago"
    if age_s < 3600:
        return f"{age_s // 60}m ago"
    return f"{age_s // 3600}h ago"


def _marks(entry: dict) -> list[str]:
    marks = []
    if entry.get("origin") == "typed":
        marks.append("typed")
    age = int(entry.get("age_s") or 0)
    if age >= STALE_AFTER_S:
        marks.append(_fmt_age(age))
    return marks


def format_transcript(result: dict) -> str:
    """Render a listen payload for an agent: the backlog is marked with
    age/origin so a slow agent sees WHAT is stale instead of an
    undifferentiated wall of text (live-test bug); the last line is
    always the current instruction. Review items riding `queued`
    (SPEC-WORKBENCH §4.2) render with the review formatter."""
    lines = []
    for q in result.get("queued", []):
        if q.get("kind") in ("finding", "ask"):
            lines.append(format_review_item(q))
            continue
        note = ", ".join(["queued while working", *_marks(q)])
        lines.append(f"[{note}] {q['text']}")
    marks = _marks(result)
    main = result.get("text", "")
    lines.append(f"[{', '.join(marks)}] {main}" if marks else main)
    return "\n".join(lines)


# ---- review items (SPEC-WORKBENCH §4.2) — the workbench wake ------------------

REVIEW_FOOTER_CLI = (
    "respond when done: `voco review status <f-id> "
    "addressed|disputed|wont-fix [--note TEXT] [--commit SHA]` for findings, "
    "`voco review reply <id> <markdown>` for questions and asks."
)


def format_review_item(item: dict) -> str:
    """One agent-facing line per review item ({kind, id, finding|ask})."""
    if item.get("kind") == "ask":
        a = item.get("ask") or {}
        return f"[review ask {item.get('id')}] {a.get('text', '')}"
    f = item.get("finding") or {}
    anchor = f.get("anchor") or {}
    marks = [f.get("kind", "concern")]
    if f.get("blocking"):
        marks.append("blocking")
    loc = ""
    if anchor.get("file"):
        loc = f" {anchor['file']}:{anchor.get('startLine', '?')}"
        end = anchor.get("endLine")
        if end and end != anchor.get("startLine"):
            loc += f"-{end}"
    head = f"[review finding {item.get('id')}, {', '.join(marks)}{loc}]"
    return f"{head} {f.get('text', '')}"


def format_review(result: dict, footer: str = REVIEW_FOOTER_CLI) -> str:
    """Render a {status: review} wake: the human flagged items on the
    workspace — they are the agent's next instruction (§4.2)."""
    lines = ["[review] the user flagged items on your workspace:"]
    lines += [format_review_item(i) for i in result.get("items", [])]
    if footer:
        lines.append(footer)
    return "\n".join(lines)


def _git(args: list[str], cwd: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=2
        )
        return out.stdout.strip() or None if out.returncode == 0 else None
    except Exception:
        return None


def _instance() -> str | None:
    """Stable per-agent-instance discriminator: two agents in one cwd must
    not collapse into one session (live-test bug). The tmux pane wins — it
    is inherited by everything the agent spawns and keeps the session
    stable across conversation restarts in the same pane; Claude Code's
    session id (also inherited by its MCP servers) covers non-tmux; None
    falls back to the legacy (host, cwd, harness) key."""
    # VOCO_INSTANCE wins: a daemon-spawned pty bakes its handle here so
    # the session links back to its terminal (W4).
    return (
        os.environ.get("VOCO_INSTANCE")
        or os.environ.get("TMUX_PANE")
        or os.environ.get("CLAUDE_CODE_SESSION_ID")
    )


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
        # Worktree siblings share this; the workbench rail groups by it.
        "common_dir": _git(
            ["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd
        )
        if repo_root
        else None,
        "harness": harness,
        "pid": os.getpid(),
        "instance": _instance(),
        # Inside tmux? Enables the inject capability (SPEC v2 → now).
        "tmux_pane": os.environ.get("TMUX_PANE"),
    }


def control(
    client: Client,
    cmd: str,
    payload: dict,
    timeout: float = 35.0,
    render=None,
) -> int:
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
    if render is not None:
        render(result)
    else:
        print(json.dumps(result))
    return 0


class Client:
    def __init__(self, base_url: str = DEFAULT_URL, token: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token or os.environ.get("VOCO_TOKEN")
        # Poller identity: lets the daemon tell "the same listener
        # re-arming its slice" from "a NEW listener taking over" — the
        # old one is superseded once instead of ping-ponging forever.
        self._poller = f"{os.getpid():x}-{secrets.token_hex(4)}"
        # Current derived identity, refreshed by session(); rides every
        # workspace verb so the daemon never resolves against a stale
        # register-time root (dogfood failure, 2026-07-06).
        self._identity: dict | None = None

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
        # FULL cwd in the key (as a short hash): two checkouts sharing a
        # basename must never share a session — /a/proj and /b/proj
        # collided here and every verb then resolved against the wrong
        # workspace root (dogfood failure, 2026-07-06).
        cwd = str(identity["cwd"])
        cwd_tag = hashlib.sha1(cwd.encode()).hexdigest()[:8]
        key = f"{identity['host']}-{Path(cwd).name}-{cwd_tag}-{identity['harness']}"
        inst = identity.get("instance")
        if inst:
            key += "-" + re.sub(r"[^A-Za-z0-9._-]", "_", str(inst))
        return CACHE_DIR / f"session-{key}.json"

    def session(self) -> dict:
        identity = derive_identity()
        self._identity = identity
        cache = self._cache_path(identity)
        if cache.exists():
            try:
                return json.loads(cache.read_text())
            except json.JSONDecodeError:
                pass
        return self.register(identity)

    def register(self, identity: dict | None = None) -> dict:
        identity = identity or derive_identity()
        self._identity = identity
        info = self._request(
            "POST",
            "/v1/bridge/register",
            # `review` opts in to workbench wakes (SPEC-WORKBENCH §4.2) —
            # this adapter ships the review verbs, so it declares them.
            {**identity, "capabilities": ["say", "listen", "screen", "review"]},
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

    def _identity_param(self) -> str:
        """`&identity=…` for GET verbs — the same re-asserted identity
        POST bodies carry."""
        if not self._identity:
            return ""
        return "&identity=" + urllib.parse.quote(json.dumps(self._identity))

    # ---- verbs ---------------------------------------------------------------------

    def say(self, text: str) -> str:
        try:
            self._with_session(
                lambda sid: self._request(
                    "POST",
                    "/v1/bridge/say",
                    {"session_id": sid, "text": text, "identity": self._identity},
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
                        "identity": self._identity,
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
                    f"/v1/bridge/listen?session_id={urllib.parse.quote(sid)}"
                    f"&poller={urllib.parse.quote(self._poller)}"
                    f"{self._identity_param()}",
                    timeout=65,
                )
            )
        except Exception:
            time.sleep(2)
            return {"status": "rearm", "_synthesized": True}

    # ---- review verbs (SPEC-WORKBENCH §4.1/§4.2) ------------------------------
    # These raise on failure (unlike say/listen): they run in an agent's
    # deliberate tool call, where a clear error beats a silent no-op.

    def findings(self, *, pending: bool = True) -> dict:
        """This session's workspace ledger: {workspace, findings, asks}."""
        q = "&pending=1" if pending else ""
        return self._with_session(
            lambda sid: self._request(
                "GET",
                f"/v1/bridge/findings?session_id={urllib.parse.quote(sid)}{q}"
                f"{self._identity_param()}",
                timeout=10,
            )
        )

    def finding_status(
        self,
        finding_id: str,
        status: str,
        *,
        note: str | None = None,
        commit: str | None = None,
    ) -> dict:
        body: dict = {"finding_id": finding_id, "status": status}
        if note is not None:
            body["note"] = note
        if commit is not None:
            body["commit"] = commit
        return self._with_session(
            lambda sid: self._request(
                "POST",
                "/v1/bridge/finding_status",
                {**body, "session_id": sid, "identity": self._identity},
                timeout=10,
            )
        )

    def reply(self, item_id: str, markdown: str) -> dict:
        """Answer an ask (a-…) or a question-kind finding (f-…) in markdown."""
        key = "ask_id" if item_id.startswith("a-") else "finding_id"
        return self._with_session(
            lambda sid: self._request(
                "POST",
                "/v1/bridge/ask_reply",
                {
                    key: item_id,
                    "markdown": markdown,
                    "session_id": sid,
                    "identity": self._identity,
                },
                timeout=10,
            )
        )

    def page_push(self, body: dict) -> dict:
        """Push a doc or diff page to this session's workspace (§3.2)."""
        return self._with_session(
            lambda sid: self._request(
                "POST",
                "/v1/bridge/page",
                {**body, "session_id": sid, "identity": self._identity},
                timeout=35,
            )
        )

    def listen(self) -> dict:
        """Park until a transcript or review wake arrives; self-heals
        through daemon restarts up to RETRY_WINDOW_S of sustained failure."""
        failing_since: float | None = None
        while True:
            result = self.listen_once()
            if result.get("status") in ("transcript", "review", "detach", "superseded"):
                return result
            # rearm: distinguish daemon-alive rearm from synthesized ones
            # by probing registration cheaply every loop is overkill; the
            # sustained-failure window only advances on socket errors.
            if result.get("_synthesized"):
                failing_since = failing_since or time.monotonic()
                if time.monotonic() - failing_since > RETRY_WINDOW_S:
                    return {"status": "unavailable"}
            else:
                failing_since = None


def listen_stream(client) -> int:
    """`voco listen --stream`: print every transcript as it arrives, never
    returning while the daemon lives. Only for harnesses that surface
    live background stdout — harnesses that wake on task EXIT (Claude
    Code) should use plain one-shot `voco listen` in a background task
    and re-run it per transcript (what voice_init hands out)."""
    while True:
        result = client.listen()
        if result.get("status") == "transcript":
            print(format_transcript(result), flush=True)
        elif result.get("status") == "review":
            print(format_review(result), flush=True)
        else:
            print(terminal_message(result) or SOFT_FAIL, flush=True)
            return 0


def _http_error(e: Exception) -> str:
    """The server's plain-text error body when there is one (agent-facing
    hints like 'finding not in this session's workspace'), else str(e)."""
    if isinstance(e, urllib.error.HTTPError):
        try:
            body = e.read().decode(errors="replace").strip()
            if body:
                return body
        except Exception:
            pass
    return str(e)


def cmd_review(args, client: Client) -> int:
    """`voco review …` — the agent's half of the findings round-trip."""
    try:
        if args.rcmd == "findings":
            r = client.findings(pending=not args.all)
            items = [
                format_review_item(
                    {"kind": "finding", "id": f.get("finding_id"), "finding": f}
                )
                for f in r.get("findings", [])
            ] + [
                format_review_item({"kind": "ask", "id": a.get("ask_id"), "ask": a})
                for a in r.get("asks", [])
            ]
            if not items:
                print("no pending review items" if not args.all else "no findings")
                return 0
            print("\n".join(items))
            print(REVIEW_FOOTER_CLI)
            return 0
        if args.rcmd == "status":
            r = client.finding_status(
                args.finding_id, args.status, note=args.note, commit=args.commit
            )
            print(f"{args.finding_id} → {r.get('finding', {}).get('status')}")
            return 0
        if args.rcmd == "reply":
            client.reply(args.id, args.markdown)
            print("answered")
            return 0
        # export: resolve this session's workspace, then the control verb.
        ws_key = client.findings().get("workspace")
        return control(
            client,
            "review.export",
            {"workspace": ws_key, "out": args.out},
            timeout=15,
            render=lambda r: print(
                f"exported {r.get('count')} finding(s) → {r.get('out')}"
            ),
        )
    except Exception as e:
        print(f"error: {_http_error(e)}", file=sys.stderr)
        return 1


def cmd_page(args, client: Client) -> int:
    """`voco page …` — push a doc or diff page (SPEC-WORKBENCH §3.2).
    Paths resolve to absolute HERE (the agent's cwd may be a subdir of
    the workspace root the daemon confines against)."""
    if args.pcmd == "doc":
        body: dict = {"type": "doc", "path": str(Path(args.path).resolve())}
        if args.name:
            body["name"] = args.name
    else:
        if args.pr is not None:
            source: dict = {"pr": args.pr}
        elif args.staged:
            source = {"staged": True}
        elif args.file:
            source = {"diff_file": str(Path(args.file).resolve())}
        else:
            # --branch BASE, bare --branch, or no flag: branch vs BASE
            # (empty ⇒ the repo's default branch, resolved server-side).
            source = {"branch": args.branch or ""}
        body = {"type": "diff", "source": source}
    try:
        r = client.page_push(body)
        where = f" → {r['root']}" if r.get("root") else ""
        print(f"page {r.get('page_id')} rev {r.get('rev')}{where}")
        return 0
    except Exception as e:
        print(f"error: {_http_error(e)}", file=sys.stderr)
        return 1


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


def cmd_doctor(client: Client) -> int:
    """Environment diagnostic: what works, what's missing, how to fix it.
    Warnings don't fail; only a dead required piece exits non-zero."""
    import importlib.util
    import shutil

    failures = 0

    def row(status: str, name: str, detail: str) -> None:
        print(f"  {status:<4} {name:<14} {detail}")

    def probe_tts(tts_cfg: dict) -> str | None:
        """POST a real tiny synth and require audio bytes back — a random
        HTTP listener squatting the port (OrbStack does) must not read ok."""
        # Default mirrors the daemon's built-in (voice_loop): port 8880,
        # the bundled floor's own default (P3 unification).
        url = f"{tts_cfg.get('base_url', 'http://127.0.0.1:8880/v1').rstrip('/')}"
        body = json.dumps(
            {
                "model": tts_cfg.get("model", "kokoro"),
                "voice": tts_cfg.get("voice", "af_heart"),
                "input": "hi",
                "response_format": "pcm",
            }
        ).encode()
        try:
            req = urllib.request.Request(
                f"{url}/audio/speech",
                data=body,
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read(4096)
            return None if len(data) >= 1000 else "answers but returns no audio"
        except Exception as e:
            return str(getattr(e, "reason", e))

    def probe_mate(base: str) -> str | None:
        """GET /models and require OpenAI-shaped JSON back."""
        try:
            req = urllib.request.Request(f"{base.rstrip('/')}/models", method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                obj = json.loads(resp.read(65536).decode())
            if isinstance(obj, dict) and ("data" in obj or "object" in obj):
                return None
            return "answers but doesn't look OpenAI-compatible"
        except Exception as e:
            return str(getattr(e, "reason", e))

    print(f"voco doctor — {client.base_url}")

    # 1. daemon, then config separately — a config.get hiccup must not
    # contradict an already-printed ok daemon row.
    cfg: dict = {}
    daemon_up = False
    try:
        state = client._request("POST", "/v1/control/state.get", {}, timeout=3)
        n = len(state.get("sessions", []))
        active = state.get("active_session")
        row("ok", "daemon", f"up; {n} session(s), active={'yes' if active else 'no'}")
        daemon_up = True
    except Exception as e:
        row("FAIL", "daemon", f"unreachable ({e}) — start with: voco-d")
        failures += 1
    if daemon_up:
        try:
            cfg = client._request("POST", "/v1/control/config.get", {}, timeout=3)
        except Exception as e:
            row("warn", "config", f"config.get failed ({e}); probing defaults")

    # 2. TTS endpoint (from daemon config when available): must return audio
    tts_cfg = cfg.get("tts", {})
    tts_url = tts_cfg.get("base_url", "http://127.0.0.1:8000/v1")
    err = probe_tts(tts_cfg)
    if err is None:
        row("ok", "tts", f"{tts_url} (synthesized a test phrase)")
    else:
        row(
            "warn",
            "tts",
            f"{tts_url}: {err} — voice will be silent"
            " (start voco-tts-floor or mlx-audio)",
        )

    # 3. first mate (llama-server or any OpenAI-compatible host)
    mate_url = cfg.get("first_mate", {}).get("base_url")
    if mate_url:
        err = probe_mate(mate_url)
        if err is None:
            row("ok", "first_mate", mate_url)
        else:
            row(
                "warn",
                "first_mate",
                f"{mate_url}: {err} — degraded mode (phrase table + forward-verbatim)",
            )
    else:
        row("--", "first_mate", "not configured (degraded mode by design)")

    # 4. tmux / inject
    if shutil.which("tmux"):
        inside = (
            "this shell CAN inject"
            if os.environ.get("TMUX_PANE")
            else ("run agents inside tmux to enable inject")
        )
        row("ok", "tmux", inside)
    else:
        row("warn", "tmux", "not installed — no managed sessions, no inject")

    # 5. listener script (voice_init output) — a stale MCP server keeps
    # writing the pre-rework streaming variant, which never exits and so
    # never wakes the agent (live-test find).
    script = CACHE_DIR / "listen.sh"
    if script.exists():
        try:
            stale = "--stream" in script.read_text()
        except OSError:
            stale = False
        if stale:
            row(
                "warn",
                "listen.sh",
                "stale streaming script — restart the agent's MCP server"
                " (old voice_init), then call voice_init again",
            )
        else:
            row("ok", "listen.sh", "one-shot listener script")
    else:
        row("--", "listen.sh", "not written yet (voice_init creates it)")

    # 6. optional python extras
    for mod, extra, why in (
        ("faster_whisper", "stt", "speech-to-text"),
        ("sounddevice", "(core)", "mic/speaker"),
        ("pynput", "ptt", "push-to-talk hotkey"),
        ("openwakeword", "wake", "wake-word"),
        ("kokoro_onnx", "floor", "bundled TTS floor"),
    ):
        found = importlib.util.find_spec(mod) is not None
        row(
            "ok" if found else "--",
            mod,
            why if found else f"{why} — uv sync --extra {extra}",
        )

    print(f"\n{'all required pieces up' if not failures else 'FAIL: daemon down'}")
    return 1 if failures else 0


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
    p_listen = sub.add_parser("listen")
    p_listen.add_argument(
        "--stream",
        action="store_true",
        help="never return: print each transcript as a stdout line"
        " (run it as a background task; keeps the agent turn free)",
    )
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
    p_new.add_argument(
        "--backend",
        choices=["tmux", "pty"],
        default=None,
        help="terminal backend (default: daemon config, else tmux)",
    )
    p_new.add_argument(
        "--worktree",
        metavar="BRANCH",
        default=None,
        help="spawn in a fresh sibling worktree on BRANCH (created if new)",
    )
    p_new.add_argument(
        "--from",
        dest="worktree_from",
        metavar="BASE",
        default=None,
        help="base ref for a NEW worktree branch (default: current HEAD)",
    )
    p_kill = sub.add_parser("kill")
    p_kill.add_argument("name", help="tmux session name (voco-...)")
    p_kill.add_argument("--host", default=None)
    p_panes = sub.add_parser("panes")
    p_panes.add_argument("--host", default=None)
    p_detach = sub.add_parser("detach")
    p_detach.add_argument("name", help="session call name (see `voco sessions`)")
    p_peek = sub.add_parser("peek")
    p_peek.add_argument(
        "name", help="session call name, or raw tmux target (voco-... / %%N)"
    )
    p_peek.add_argument("--host", default=None)
    p_review = sub.add_parser("review", help="findings round-trip + export")
    rsub = p_review.add_subparsers(dest="rcmd", required=True)
    r_ls = rsub.add_parser("findings", help="list pending review items")
    r_ls.add_argument("--all", action="store_true", help="include resolved")
    r_st = rsub.add_parser("status", help="report a finding round-trip")
    r_st.add_argument("finding_id")
    r_st.add_argument("status", choices=["addressed", "disputed", "wont-fix"])
    r_st.add_argument("--note", default=None)
    r_st.add_argument("--commit", default=None)
    r_re = rsub.add_parser("reply", help="answer an ask or question finding")
    r_re.add_argument("id", help="a-… (ask) or f-… (question finding)")
    r_re.add_argument("markdown")
    r_ex = rsub.add_parser("export", help="write the review JSON + sidecar")
    r_ex.add_argument("--out", default=None)
    p_page = sub.add_parser("page", help="push a doc/diff page (workbench)")
    psub = p_page.add_subparsers(dest="pcmd", required=True)
    pg_doc = psub.add_parser("doc")
    pg_doc.add_argument("path")
    pg_doc.add_argument("--name", default=None)
    pg_diff = psub.add_parser("diff")
    pg_diff.add_argument("--pr", default=None, help="GitHub PR number (needs gh)")
    pg_diff.add_argument(
        "--branch",
        nargs="?",
        const="",
        default=None,
        help="diff vs BASE (default: the repo's default branch)",
    )
    pg_diff.add_argument("--staged", action="store_true")
    pg_diff.add_argument("--file", default=None, help="a unified-diff file")
    sub.add_parser("watch")
    p_input = sub.add_parser("input")  # typed input path (say_as_user)
    p_input.add_argument("text")
    sub.add_parser("attach-cmd")
    sub.add_parser("doctor")
    # lifecycle (BUILD-PROD P1): the daemon as a managed process
    p_up = sub.add_parser("up", help="start the daemon (managed, detached)")
    p_up.add_argument("--config", default=None)
    p_up.add_argument("--port", type=int, default=7777)
    p_up.add_argument("--no-audio", action="store_true")
    p_up.add_argument(
        "--wait", type=float, default=20.0, help="seconds to wait for health"
    )
    p_up.add_argument(
        "--verbose", action="store_true", help="daemon DEBUG-level logging"
    )
    p_down = sub.add_parser("down", help="stop the managed daemon")
    p_down.add_argument("--port", type=int, default=7777)
    p_logs = sub.add_parser("logs", help="show the managed daemon's log")
    p_logs.add_argument("-n", "--lines", type=int, default=50)
    p_logs.add_argument("-f", "--follow", action="store_true")
    p_auto = sub.add_parser(
        "autostart", help="run the daemon at login (launchd on macOS)"
    )
    p_auto.add_argument("action", choices=["install", "uninstall", "status"])
    p_auto.add_argument("--config", default=None)
    p_auto.add_argument("--port", type=int, default=7777)
    p_cfg = sub.add_parser("config")
    p_cfg.add_argument("action", choices=["get", "set"])
    p_cfg.add_argument("key", nargs="?", help="section.key (set only)")
    p_cfg.add_argument("value", nargs="?", help="value; JSON parsed, else string")
    args = parser.parse_args()

    # Lifecycle commands manage the daemon PROCESS — they never need a
    # session, must work while the daemon is down, and are LOCAL by
    # nature (they ignore VOCO_URL: that aims clients, never signals).
    if args.cmd in ("up", "down", "logs", "autostart"):
        from voco_cli import lifecycle

        if args.cmd == "up":
            raise SystemExit(lifecycle.cmd_up(args))
        if args.cmd == "down":
            raise SystemExit(lifecycle.cmd_down(args))
        if args.cmd == "logs":
            raise SystemExit(lifecycle.cmd_logs(args))
        raise SystemExit(lifecycle.cmd_autostart(args))

    client = Client()
    if args.cmd == "say":
        print(client.say(args.text))
    elif args.cmd == "listen":
        if args.stream:
            sys.exit(listen_stream(client))
        result = client.listen()
        if result.get("status") == "transcript":
            print(format_transcript(result))
        elif result.get("status") == "review":
            print(format_review(result))
        else:
            # Say WHY it ended: a user detach must not read as a crash.
            print(terminal_message(result) or SOFT_FAIL)
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
        payload = {
            "harness": args.harness,
            "name": args.name,
            "cwd": args.cwd,
            "host": args.host,
        }
        if args.backend:
            payload["backend"] = args.backend
        if args.worktree:
            # The daemon needs a repo to branch from; default to here.
            payload["cwd"] = args.cwd or os.getcwd()
            payload["worktree"] = {"branch": args.worktree}
            if args.worktree_from:
                payload["worktree"]["from"] = args.worktree_from
        sys.exit(control(client, "session.spawn", payload))
    elif args.cmd == "kill":
        sys.exit(
            control(client, "session.kill", {"name": args.name, "host": args.host})
        )
    elif args.cmd == "panes":
        sys.exit(control(client, "session.panes", {"host": args.host}))
    elif args.cmd == "detach":
        sys.exit(control(client, "session.detach", {"name": args.name}, timeout=5))
    elif args.cmd == "peek":
        # Call names go through the registry; voco-*/% targets hit tmux raw.
        raw = args.name.startswith(("voco-", "%"))
        payload = (
            {"target": args.name, "host": args.host} if raw else {"name": args.name}
        )
        sys.exit(
            control(
                client,
                "session.peek",
                payload,
                timeout=15,
                render=lambda r: print(r.get("text", ""), end=""),
            )
        )
    elif args.cmd == "review":
        sys.exit(cmd_review(args, client))
    elif args.cmd == "page":
        sys.exit(cmd_page(args, client))
    elif args.cmd == "watch":
        sys.exit(cmd_watch(client))
    elif args.cmd == "input":
        sys.exit(control(client, "say_as_user", {"text": args.text}, timeout=5))
    elif args.cmd == "attach-cmd":
        sys.exit(cmd_attach(args, client))
    elif args.cmd == "doctor":
        sys.exit(cmd_doctor(client))
    elif args.cmd == "config":
        if args.action == "get":
            sys.exit(control(client, "config.get", {}, timeout=5))
        if not args.key or args.value is None:
            print("usage: voco config set <section.key> <value>", file=sys.stderr)
            sys.exit(2)
        try:
            value = json.loads(args.value)  # numbers/bools; fallback: string
        except json.JSONDecodeError:
            value = args.value
        sys.exit(
            control(client, "config.set", {"key": args.key, "value": value}, timeout=5)
        )


if __name__ == "__main__":
    main()
