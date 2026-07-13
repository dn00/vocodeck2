"""Installed-wheel smoke: daemon HTTP plus MCP stdio and review round-trip."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from importlib.metadata import metadata
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def request(base: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=5) as response:
        return json.loads(response.read())


def fetch_text(base: str, path: str) -> str:
    with urllib.request.urlopen(base + path, timeout=5) as response:
        return response.read().decode("utf-8")


def wait_healthy(base: str, daemon: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if daemon.poll() is not None:
            raise RuntimeError(f"daemon exited early ({daemon.returncode})")
        try:
            health = request(base, "/v1/health")
            if health.get("ok") and health.get("service") == "voco-d":
                return
        except Exception:
            time.sleep(0.05)
    raise RuntimeError("daemon did not become healthy")


def result_text(result) -> str:
    assert not result.isError, result
    assert result.content, result
    return str(result.content[0].text)


async def exercise_mcp(executable: Path, base: str, repo: Path, cache: Path) -> None:
    env = dict(os.environ)
    env.update(
        {
            "VOCO_URL": base,
            "VOCO_CACHE": str(cache),
            "VOCO_INSTANCE": "wheel-mcp-smoke",
        }
    )
    params = StdioServerParameters(command=str(executable), env=env, cwd=repo)
    async with stdio_client(params) as (read, write):  # noqa: SIM117
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            assert {
                "voice_init",
                "voice_screen",
                "page_push",
                "page_close",
                "review_findings",
                "review_reply",
            } <= names

            init = result_text(await session.call_tool("voice_init", {}))
            assert "You are" in init and "HOW TO LISTEN" in init
            screen = result_text(
                await session.call_tool(
                    "voice_screen",
                    {"markdown": "# Wheel smoke", "title": "Installed MCP"},
                )
            )
            assert screen == "ok"
            pushed = result_text(
                await session.call_tool(
                    "page_push",
                    {
                        "content": "# Release page\n\ninstalled wheel",
                        "name": "Release page",
                    },
                )
            )
            assert "published: page" in pushed

            snapshot = request(base, "/v1/control/state.get", {})
            workspace = next(
                item for item in snapshot["workspaces"] if item["root"] == str(repo)
            )
            page = next(
                item for item in workspace["pages"] if item["title"] == "Release page"
            )
            added = request(
                base,
                "/v1/control/finding.add",
                {
                    "workspace": workspace["key"],
                    "page_id": page["page_id"],
                    "anchor": {"block": 0, "text": "installed wheel"},
                    "text": "Confirm the installed MCP can close this loop",
                    "kind": "concern",
                },
            )
            finding_id = added["finding"]["finding_id"]
            pending = result_text(await session.call_tool("review_findings", {}))
            assert finding_id in pending and "installed MCP" in pending
            reply = result_text(
                await session.call_tool(
                    "review_reply",
                    {"id": finding_id, "status": "addressed", "commit": "wheel-smoke"},
                )
            )
            assert "status → addressed" in reply
            ledger = request(
                base,
                "/v1/control/finding.list",
                {"workspace": workspace["key"]},
            )
            finding = next(
                item for item in ledger["findings"] if item["finding_id"] == finding_id
            )
            assert finding["status"] == "addressed"


def main() -> None:
    scripts = Path(sys.executable).parent
    suffix = ".exe" if os.name == "nt" else ""
    voco_d = scripts / f"voco-d{suffix}"
    voco_mcp = scripts / f"voco-mcp{suffix}"
    voco_cli = scripts / f"voco{suffix}"
    assert voco_d.is_file() and voco_mcp.is_file() and voco_cli.is_file()
    subprocess.run([str(voco_cli), "--help"], check=True, stdout=subprocess.DEVNULL)
    extras = set(metadata("voco").get_all("Provides-Extra") or [])
    assert {"dev", "floor", "mcp", "ptt", "stt", "wake"} <= extras

    with tempfile.TemporaryDirectory(prefix="voco-wheel-smoke-") as temp:
        root = Path(temp)
        repo = root / "repo"
        repo.mkdir()
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=repo,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        subprocess.run(
            ["git", "config", "user.email", "smoke@example.invalid"],
            cwd=repo,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Voco Smoke"], cwd=repo, check=True
        )
        (repo / "README.md").write_text("# smoke\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-m", "seed"],
            cwd=repo,
            check=True,
            stdout=subprocess.DEVNULL,
        )

        port = free_port()
        base = f"http://127.0.0.1:{port}"
        config = root / "config.toml"
        config.write_text(
            f"[state]\ndir = {json.dumps(str(root / 'state'))}\n"
            f"[workbench]\ndata_dir = {json.dumps(str(root / 'workbench'))}\n"
            "live_git_s = 0\n",
            encoding="utf-8",
        )
        daemon = subprocess.Popen(
            [str(voco_d), "--config", str(config), "--no-audio", "--port", str(port)],
            cwd=repo,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            wait_healthy(base, daemon)
            index = fetch_text(base, "/")
            client = fetch_text(base, "/static/app.mjs")
            assert "/static/app.mjs" in index
            assert "Workbench entry" in client
            asyncio.run(exercise_mcp(voco_mcp, base, repo, root / "cache"))
        finally:
            if daemon.poll() is None:
                daemon.send_signal(signal.SIGTERM)
                try:
                    daemon.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    daemon.kill()
            if daemon.returncode not in (0, -signal.SIGTERM):
                stderr = daemon.stderr.read() if daemon.stderr else ""
                raise RuntimeError(f"daemon exited {daemon.returncode}: {stderr}")


if __name__ == "__main__":
    main()
