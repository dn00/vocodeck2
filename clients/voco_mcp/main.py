"""voco-mcp — stdio MCP server exposing the voice bridge (SPEC §8.4).

ROLE: the preferred harness integration (Claude Code, Codex, opencode):
three tools over the same daemon HTTP the CLI uses. Thin: all transport
logic (identity derivation, token cache, 410 re-register, fail-soft,
synthesized rearm) is reused from voco_cli.main.Client.

INVARIANTS: voice_listen loops internally and returns within
VOCO_MCP_LISTEN_BUDGET_S (default 240s — stay under harness tool
timeouts; the tool description tells the agent to call again on a
'nothing yet' result, so the parked loop survives any budget). Tool
results are plain text; errors are soft one-liners, never exceptions
(fail-silent toward the agent, SPEC §1.2).
"""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
import time

from voco_cli.main import (
    CACHE_DIR,
    SOFT_FAIL,
    Client,
    format_review,
    format_review_item,
    format_transcript,
)

LISTEN_BUDGET_S = float(os.environ.get("VOCO_MCP_LISTEN_BUDGET_S", "240"))

REVIEW_FOOTER_MCP = (
    "address each item, then report via review_reply (status for findings, "
    "markdown for questions and asks); re-list any time with review_findings."
)

INSTRUCTIONS = """\
You are connected to a voice daemon. The user often listens instead of
reading. Call voice_say with 1-3 short plain sentences for anything they
should hear (no markdown, paths, or code in speech). Put anything
substantial on the screen with voice_screen, then say a one-line summary.
Speak brief progress updates during long work.

The deck speaks immediate acknowledgments itself (earcon + a local voice
tier). NEVER voice_say a bare acknowledgment like "got it" or "on it" —
speak only substance: results, findings, questions, blockers.

Listening — pick ONE mode:
- If you can run background shell tasks: call voice_init ONCE at the
  start; it returns the exact `bash <path>` command to run as a
  background task. The task exits when the user speaks and its output is
  their instruction — act on it, then run the same command again in the
  background. You stay free to work and react to typed input meanwhile.
  Do not also call voice_listen.
- Otherwise: when your turn's work is complete, END by calling
  voice_listen and acting on what it returns; if it returns 'nothing
  yet', call voice_listen again. Treat returned transcripts as the
  user's next instruction.

The user also reviews your work in a browser workbench (diffs, docs you
push with page_push). Treat review findings and questions as user input:
address them, then report via review_reply. They arrive through the same
listening loop; review_findings re-lists them any time.
"""


def init_reply(client: Client) -> str:
    """voice_init: write the listener script, return complete integration
    instructions.

    Generates TWO scripts:
    - listen.sh (one-shot): exits after one transcript. For agents that
      only support background-task-with-completion (run → get output →
      re-run).
    - listen-stream.sh (streaming): prints one line per transcript, never
      exits. For agents with a Monitor/event-stream primitive (Claude Code).

    A script file beats an inline command: the agent's shell may be fish
    (no VAR=x prefix syntax), lacks `voco` on PATH (the CLI lives in this
    server's venv), and the bearer token stays inside a 0700 file instead
    of process args. Identity is NOT baked in — the running script
    inherits the agent's TMUX_PANE / harness session id, so it registers
    as the SAME session as this server. Safe to run again at any time:
    a new run supersedes the previous listener."""
    try:
        sess = client.session()
    except Exception:
        return SOFT_FAIL

    env_lines = [f"export VOCO_URL={shlex.quote(client.base_url)}"]
    if client.token:
        env_lines.append(f"export VOCO_TOKEN={shlex.quote(client.token)}")
    env_block = "\n".join(env_lines)
    py = shlex.quote(sys.executable)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # One-shot script
    oneshot = CACHE_DIR / "listen.sh"
    oneshot.write_text(
        "#!/usr/bin/env bash\n"
        "# voco listener (one-shot) — exits after one transcript.\n"
        f"{env_block}\n"
        f"exec {py} -m voco_cli.main listen\n",
        encoding="utf-8",
    )
    oneshot.chmod(0o700)

    # Streaming script
    stream = CACHE_DIR / "listen-stream.sh"
    stream.write_text(
        "#!/usr/bin/env bash\n"
        "# voco listener (streaming) — one stdout line per transcript,\n"
        "# never exits. Use with Monitor or equivalent event stream.\n"
        f"{env_block}\n"
        f"exec {py} -m voco_cli.main listen --stream\n",
        encoding="utf-8",
    )
    stream.chmod(0o700)

    return (
        f"You are {sess['call_name']} on the voice deck.\n\n"
        "HOW TO LISTEN — pick the best mode for your harness:\n\n"
        "## Streaming (preferred — Claude Code Monitor, or any event stream)\n"
        "Run this ONCE as a persistent monitor:\n\n"
        f"  bash {shlex.quote(str(stream))}\n\n"
        "Each stdout line is one transcript — the user's spoken instruction.\n"
        "Act on each line as it arrives. The script never exits; stop it\n"
        "when the session ends.\n\n"
        "## One-shot (fallback — background task with completion notification)\n"
        "Run this as a background task:\n\n"
        f"  bash {shlex.quote(str(oneshot))}\n\n"
        "It exits when the user speaks; its output is their instruction.\n"
        "Act on it, then re-run the same command to keep listening.\n\n"
        "## Rules\n"
        "- Do NOT call voice_listen while a listener script runs.\n"
        "- If the output says the session was ended, superseded, or the\n"
        "  daemon is unreachable, STOP re-running.\n"
        "- Speak with voice_say. Show content with voice_screen.\n"
        "- Lines starting with [review] are workbench findings/questions\n"
        "  the user flagged — treat them as user input: address them,\n"
        "  then report via review_reply (or `voco review ...`).\n"
        "- The user is listening, not reading the terminal."
    )


def build_server():
    from mcp.server import Server
    from mcp.types import TextContent, Tool

    client = Client()
    server = Server("voco", instructions=INSTRUCTIONS)

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="voice_say",
                description=(
                    "Speak 1-3 short plain sentences aloud to the user. No "
                    "markdown, file paths, or code — it is read by TTS."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            ),
            Tool(
                name="voice_screen",
                description=(
                    "Show substantial content (lists, diffs, code, plans) on "
                    "the user's screen as markdown. Use mode 'append' to "
                    "extend the current screen."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "markdown": {"type": "string"},
                        "title": {"type": "string"},
                        "mode": {"type": "string", "enum": ["show", "append"]},
                    },
                    "required": ["markdown"],
                },
            ),
            Tool(
                name="voice_init",
                description=(
                    "Set up hands-free listening: registers you on the "
                    "voice deck, writes listener scripts (streaming + "
                    "one-shot), and returns complete integration "
                    "instructions for your harness. Call ONCE at session "
                    "start; idempotent and safe to re-call. Prefer this "
                    "over voice_listen whenever you can run background "
                    "shell tasks."
                ),
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="voice_listen",
                description=(
                    "Park and wait for the user's next spoken instruction "
                    "or review items they flagged in the workbench. Call "
                    "this when your turn's work is complete. If it returns "
                    "'nothing yet', call it again to keep listening. NOTE: "
                    "this blocks your turn — if you can run background "
                    "shell tasks, call voice_init once instead and "
                    "background the command it returns."
                ),
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="page_push",
                description=(
                    "Publish a page to the user's workbench browser: a doc "
                    "(markdown file path or inline content) or a diff "
                    "(pr/branch/staged/diff file) the user can annotate. "
                    "Re-pushing the same doc/diff updates it in place."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "doc: file path inside the workspace",
                        },
                        "content": {
                            "type": "string",
                            "description": "doc: inline markdown instead of a path",
                        },
                        "name": {
                            "type": "string",
                            "description": "doc title (required for inline content)",
                        },
                        "diff": {
                            "type": "object",
                            "description": (
                                "diff page instead of a doc — exactly one of: "
                                '{"pr": N}, {"branch": "BASE"} ("" = default '
                                'branch), {"staged": true}, {"file": "x.patch"}'
                            ),
                            "properties": {
                                "pr": {"type": ["integer", "string"]},
                                "branch": {"type": "string"},
                                "staged": {"type": "boolean"},
                                "file": {"type": "string"},
                            },
                        },
                    },
                },
            ),
            Tool(
                name="page_close",
                description=(
                    "Close a non-pinned page in the user's workbench by page id. "
                    "The page remains restorable by publishing it again."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {"page_id": {"type": "string"}},
                    "required": ["page_id"],
                },
            ),
            Tool(
                name="review_findings",
                description=(
                    "List the review items (findings + questions) the user "
                    "flagged on your workspace in the workbench. Pending "
                    "only by default."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "all": {
                            "type": "boolean",
                            "description": "include resolved items",
                        }
                    },
                },
            ),
            Tool(
                name="review_reply",
                description=(
                    "Report back on a review item. For a finding (f-…): set "
                    "status addressed/disputed/wont-fix with an optional "
                    "note/commit. For a question or ask (a-…): answer in "
                    "markdown. Idempotent — safe to repeat."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "f-… or a-…"},
                        "status": {
                            "type": "string",
                            "enum": ["addressed", "disputed", "wont-fix"],
                        },
                        "markdown": {
                            "type": "string",
                            "description": "answer text (questions/asks)",
                        },
                        "note": {"type": "string"},
                        "commit": {"type": "string"},
                    },
                    "required": ["id"],
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        loop = asyncio.get_running_loop()
        if name == "voice_say":
            result = await loop.run_in_executor(
                None, client.say, str(arguments.get("text", ""))
            )
            return [TextContent(type="text", text=result)]
        if name == "voice_screen":
            result = await loop.run_in_executor(
                None,
                client.screen,
                str(arguments.get("markdown", "")),
                arguments.get("title"),
                arguments.get("mode", "show"),
            )
            return [TextContent(type="text", text=result)]
        if name == "voice_init":
            result = await loop.run_in_executor(None, init_reply, client)
            return [TextContent(type="text", text=result)]
        if name == "voice_listen":
            result = await loop.run_in_executor(None, _listen_budgeted, client)
            return [TextContent(type="text", text=result)]
        if name == "page_push":
            result = await loop.run_in_executor(None, _page_push, client, arguments)
            return [TextContent(type="text", text=result)]
        if name == "page_close":
            result = await loop.run_in_executor(None, _page_close, client, arguments)
            return [TextContent(type="text", text=result)]
        if name == "review_findings":
            result = await loop.run_in_executor(
                None, _review_findings, client, bool(arguments.get("all"))
            )
            return [TextContent(type="text", text=result)]
        if name == "review_reply":
            result = await loop.run_in_executor(None, _review_reply, client, arguments)
            return [TextContent(type="text", text=result)]
        return [TextContent(type="text", text=f"unknown tool {name}")]

    return server


def _listen_budgeted(client: Client) -> str:
    """Loop rearm slices inside one tool call, bounded by the budget."""
    from voco_cli.main import terminal_message

    deadline = time.monotonic() + LISTEN_BUDGET_S
    while time.monotonic() < deadline:
        result = client.listen_once()
        status = result.get("status")
        if status == "transcript":
            return format_transcript(result)
        if status == "review":
            return format_review(result, footer=REVIEW_FOOTER_MCP)
        if status in ("detach", "superseded"):
            return terminal_message(result) or SOFT_FAIL
        # rearm (real or synthesized): keep parking within budget
    return "nothing yet — call voice_listen again to keep listening."


def _http_hint(e: Exception) -> str:
    """Server error bodies are agent-facing hints; pass them through."""
    from urllib.error import HTTPError

    if isinstance(e, HTTPError):
        try:
            body = e.read().decode(errors="replace").strip()
            if body:
                return body
        except Exception:
            pass
    return str(e)


def _page_push(client: Client, args: dict) -> str:
    diff = args.get("diff")
    if isinstance(diff, dict):
        if "file" in diff:
            source: dict = {"diff_file": str(diff["file"])}
        elif "pr" in diff:
            source = {"pr": diff["pr"]}
        elif diff.get("staged"):
            source = {"staged": True}
        else:
            source = {"branch": str(diff.get("branch") or "")}
        body: dict = {"type": "diff", "source": source}
    elif args.get("path") or args.get("content") is not None:
        body = {"type": "doc"}
        for k in ("path", "content", "name"):
            if args.get(k) is not None:
                body[k] = args[k]
    else:
        return "page_push needs a doc (path or content+name) or a diff spec"
    try:
        r = client.page_push(body)
        where = f" in workspace {r['root']}" if r.get("root") else ""
        return f"published: page {r.get('page_id')} rev {r.get('rev')}{where}"
    except Exception as e:
        return f"page_push failed: {_http_hint(e)}"


def _page_close(client: Client, args: dict) -> str:
    page_id = str(args.get("page_id") or "").strip()
    if not page_id:
        return "page_close needs page_id"
    try:
        result = client.page_close(page_id)
        page = result.get("page") or {}
        return f"closed: page {page.get('page_id', page_id)}"
    except Exception as e:
        return f"page_close failed: {_http_hint(e)}"


def _review_findings(client: Client, all_: bool) -> str:
    try:
        r = client.findings(pending=not all_)
    except Exception as e:
        return f"review_findings failed: {_http_hint(e)}"
    lines = [
        format_review_item({"kind": "finding", "id": f.get("finding_id"), "finding": f})
        for f in r.get("findings", [])
    ] + [
        format_review_item({"kind": "ask", "id": a.get("ask_id"), "ask": a})
        for a in r.get("asks", [])
    ]
    if not lines:
        return "no pending review items" if not all_ else "no findings"
    return "\n".join([*lines, REVIEW_FOOTER_MCP])


def _review_reply(client: Client, args: dict) -> str:
    item_id = str(args.get("id", ""))
    status, markdown = args.get("status"), args.get("markdown")
    done: list[str] = []
    try:
        if markdown:
            client.reply(item_id, str(markdown))
            done.append("answered")
        if status:
            if not item_id.startswith("f-"):
                done.append("status ignored (only findings carry one)")
            else:
                client.finding_status(
                    item_id,
                    str(status),
                    note=args.get("note"),
                    commit=args.get("commit"),
                )
                done.append(f"status → {status}")
    except Exception as e:
        return f"review_reply failed: {_http_hint(e)}"
    if not done:
        return "nothing to do: pass status (finding) and/or markdown (answer)"
    return f"{item_id}: " + ", ".join(done)


def main() -> None:
    try:
        import mcp.server.stdio
    except ImportError as e:
        raise SystemExit(
            "voco-mcp requires the 'mcp' package: uv sync --extra mcp"
        ) from e

    async def run() -> None:
        server = build_server()
        async with mcp.server.stdio.stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    asyncio.run(run())


if __name__ == "__main__":
    main()
