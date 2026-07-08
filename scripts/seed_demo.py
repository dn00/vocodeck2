"""Seed a RUNNING daemon with mockup-grade demo data — see the deck full.

Builds a small demo repo (+ a sibling worktree), registers two fake
agents through the real bridge, publishes a two-rev branch diff, and
files findings/asks/links in every state the mockup shows. Everything
goes through the daemon's own HTTP surface: what you then see in the
browser is the REAL tool rendering real state, not a mock.

Run:  uv run python scripts/seed_demo.py  [--base http://127.0.0.1:7777]
Idempotent: the demo repo is rebuilt from scratch each run; re-seeded
pages/findings converge (same refs bump revs; duplicate adds pile up
findings — run against a fresh data dir if you want it pristine).

The fake agents have no live process behind them, so their state dots
decay honestly (idle → stale). That is the tool telling the truth.
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

DEMO = Path("/tmp/voco-demo")
WT = Path("/tmp/voco-demo-fix-pty")

PRESENCE_V1 = """// presence strip — the voice's permanent home
export function orbState(mic) {
  if (mic.attention === "muted") return "muted";
  return "armed";
}
"""

PRESENCE_V2 = """// presence strip — the voice's permanent home
export function orbState(mic, turn) {
  if (mic.attention === "muted") return "muted";
  if (turn === "capturing") return "hearing";
  return mic.attention === "always" ? "hot" : "armed";
}

export function captionFor(routed) {
  return routed ? `“${routed.text}”` : "listening";
}
"""

APP_V1 = """// deck entry
function renderFeed() { /* bottom strip */ }
renderFeed();
"""

APP_V2 = """// deck entry
// transcript tab replaces the feed strip (one input story)
function renderStrip() { /* presence strip */ }
renderStrip();
"""

TESTS_V1 = """def test_orb_state_muted():
    assert orb_state(attention="muted") == "muted"
"""

TESTS_V2 = """def test_orb_state_muted():
    assert orb_state(attention="muted") == "muted"


def test_orb_state_ptt_renders_armed():
    assert orb_state(attention="ptt_only") == "armed"


def test_caption_quotes_the_routed_text():
    assert caption_for("hi") == "“hi”"
"""


def sh(*argv: str, cwd: Path | None = None) -> None:
    subprocess.run(
        argv,
        cwd=cwd,
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "demo",
            "GIT_AUTHOR_EMAIL": "demo@voco",
            "GIT_COMMITTER_NAME": "demo",
            "GIT_COMMITTER_EMAIL": "demo@voco",
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "HOME": str(Path.home()),
        },
    )


def build_repo() -> None:
    if DEMO.is_dir():
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(WT)],
            cwd=DEMO,
            capture_output=True,
        )
    subprocess.run(["rm", "-rf", str(WT), str(DEMO)], check=True)
    src = DEMO / "src" / "voco" / "server" / "static"
    tests = DEMO / "tests"
    src.mkdir(parents=True)
    tests.mkdir(parents=True)
    sh("git", "init", "-q", "-b", "main", str(DEMO))
    (src / "presence.mjs").write_text(PRESENCE_V1)
    (src / "app.mjs").write_text(APP_V1)
    (tests / "test_presence.py").write_text(TESTS_V1)
    sh("git", "add", "-A", cwd=DEMO)
    sh("git", "commit", "-qm", "init", cwd=DEMO)
    sh("git", "checkout", "-qb", "workbench-strip", cwd=DEMO)
    (src / "presence.mjs").write_text(PRESENCE_V2)
    (src / "app.mjs").write_text(APP_V2)
    sh("git", "add", "-A", cwd=DEMO)
    sh("git", "commit", "-qm", "presence strip: orb states + captions", cwd=DEMO)
    # sibling worktree = a second WORK ROW in the same repo group
    sh("git", "worktree", "add", "-q", "-b", "fix-pty", str(WT), "main", cwd=DEMO)


class Api:
    def __init__(self, base: str) -> None:
        self.base = base

    def post(self, path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def control(self, cmd: str, payload: dict) -> dict:
        return self.post(f"/v1/control/{cmd}", payload)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:7777")
    args = ap.parse_args()
    api = Api(args.base)
    host = socket.gethostname().split(".")[0]

    print("· building demo repo + worktree")
    build_repo()

    print("· opening workspaces")
    ws = api.control("workspace.open", {"path": str(DEMO)})
    key = ws["workspace"]
    api.control("workspace.open", {"path": str(WT)})

    print("· linking issue/PR")
    api.control(
        "workspace.link",
        {
            "workspace": key,
            "pr": {
                "number": 7,
                "url": "https://github.com/demo/voco-demo/pull/7",
                "title": "presence strip: orb states + captions",
            },
            "issue": {
                "number": 3,
                "url": "https://github.com/demo/voco-demo/issues/3",
                "title": "voice has no visual presence",
            },
        },
    )

    print("· registering agents (real bridge)")
    freya = api.post(
        "/v1/bridge/register",
        {
            "host": host,
            "user": "demo",
            "cwd": str(DEMO),
            "worktree": str(DEMO),
            "repo": "voco-demo",
            "branch": "workbench-strip",
            "capabilities": ["say", "listen", "review"],
        },
    )
    orion = api.post(
        "/v1/bridge/register",
        {
            "host": host,
            "user": "demo",
            "cwd": str(WT),
            "worktree": str(WT),
            "repo": "voco-demo",
            "branch": "fix-pty",
            "capabilities": ["say", "listen", "review"],
        },
    )
    print(f"  agents: {freya['call_name']} (strip) · {orion['call_name']} (fix-pty)")

    print("· publishing the branch diff (rev 1, then rev 2 for interdiff)")
    api.control("page.publish", {"workspace": key, "source": {"branch": "main"}})
    (DEMO / "tests" / "test_presence.py").write_text(TESTS_V2)
    sh("git", "add", "-A", cwd=DEMO)
    sh("git", "commit", "-qm", "tests: ptt renders armed + caption quotes", cwd=DEMO)
    page = api.control("page.publish", {"workspace": key, "source": {"branch": "main"}})
    pid = page["page_id"]
    print(f"  diff {pid} at rev {page['rev']}")

    print("· screen + transcript for", freya["call_name"])
    api.post(
        "/v1/bridge/screen",
        {
            "session_id": freya["session_id"],
            "markdown": "## strip status\n- orb states: **done**\n- captions: done\n"
            "- tests: 3 green\n\nnext: wire `speech.sentence` karaoke",
            "title": "strip status",
        },
    )
    for line in (
        "Heard all of it. Ledger walked: the blocking orb finding is real "
        "and I've queued the fix.",
        "Gates are green — 3 tests, tsc clean. Re-pushed the branch diff as r2.",
    ):
        api.post("/v1/bridge/say", {"session_id": freya["session_id"], "text": line})

    print("· findings in every state")
    f1 = api.control(
        "finding.add",
        {
            "workspace": key,
            "page_id": pid,
            "anchor": {
                "file": "src/voco/server/static/presence.mjs",
                "side": "new",
                "startLine": 4,
                "endLine": 4,
            },
            "text": "orb must reflect attention mode, not just VAD — muted should "
            "read differently from idle",
            "kind": "concern",
            "blocking": True,
        },
    )
    f2 = api.control(
        "finding.add",
        {
            "workspace": key,
            "page_id": pid,
            "anchor": {
                "file": "src/voco/server/static/app.mjs",
                "side": "new",
                "startLine": 2,
                "endLine": 2,
            },
            "text": "feed strip removal needs a migration note in the README",
            "kind": "nit",
        },
    )
    api.control(
        "finding.status",
        {
            "workspace": key,
            "finding_id": f2["finding"]["finding_id"],
            "status": "addressed",
            "note": "noted in README §deck; regression test added",
        },
    )
    f3 = api.control(
        "finding.add",
        {
            "workspace": key,
            "page_id": pid,
            "anchor": {
                "file": "src/voco/server/static/presence.mjs",
                "side": "new",
                "startLine": 8,
                "endLine": 8,
            },
            "text": "should the orb also carry PTT press state, or is that the halo's job?",
            "kind": "question",
        },
    )
    api.post(
        "/v1/bridge/ask_reply",
        {
            "session_id": freya["session_id"],
            "finding_id": f3["finding"]["finding_id"],
            "markdown": "the **orb**: press state is attention-level truth, same "
            "family as muted/wake/always. Halo stays speech-only.",
        },
    )

    print("· an open ask + a queued input")
    api.control(
        "ask.create",
        {
            "workspace": key,
            "text": "want the r2 interdiff annotated file-by-file, or is the "
            "since-rev chip enough?",
        },
    )
    try:
        api.control("switch_session", {"name": freya["call_name"]})
        api.control(
            "say_as_user",
            {"text": "praise on the r2 hunk layout, flag stays open until I re-read"},
        )
    except Exception as e:  # voice routing may be busy on a live deck
        print(f"  (queued input skipped: {e})", file=sys.stderr)

    print(f"\nseeded. open {args.base} and hard-refresh (cmd+shift+R).")
    print("fake agents have no live process — their dots decay to idle/stale,")
    print("which is the deck being honest. re-run any time.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
