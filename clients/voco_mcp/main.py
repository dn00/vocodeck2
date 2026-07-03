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
import time

from voco_cli.main import SOFT_FAIL, Client

LISTEN_BUDGET_S = float(os.environ.get("VOCO_MCP_LISTEN_BUDGET_S", "240"))

INSTRUCTIONS = """\
You are connected to a voice daemon. The user often listens instead of
reading. Call voice_say with 1-3 short plain sentences for anything they
should hear (no markdown, paths, or code in speech). Put anything
substantial on the screen with voice_screen, then say a one-line summary.
Speak brief progress updates during long work. When your turn's work is
complete, END by calling voice_listen and acting on what it returns; if it
returns 'nothing yet', call voice_listen again. Treat returned transcripts
as the user's next instruction.
"""


def build_server():
    from mcp.server import Server  # noqa: PLC0415
    from mcp.types import TextContent, Tool  # noqa: PLC0415

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
                name="voice_listen",
                description=(
                    "Park and wait for the user's next spoken instruction. "
                    "Call this when your turn's work is complete. If it "
                    "returns 'nothing yet', call it again to keep listening."
                ),
                inputSchema={"type": "object", "properties": {}},
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
        if name == "voice_listen":
            result = await loop.run_in_executor(None, _listen_budgeted, client)
            return [TextContent(type="text", text=result)]
        return [TextContent(type="text", text=f"unknown tool {name}")]

    return server


def _listen_budgeted(client: Client) -> str:
    """Loop rearm slices inside one tool call, bounded by the budget."""
    deadline = time.monotonic() + LISTEN_BUDGET_S
    while time.monotonic() < deadline:
        result = client.listen_once()
        status = result.get("status")
        if status == "transcript":
            lines = [
                f"[queued while working] {q['text']}" for q in result.get("queued", [])
            ]
            lines.append(result["text"])
            return "\n".join(lines)
        if status == "detach":
            return "voice daemon shutting down — stop listening."
        # rearm (real or synthesized): keep parking within budget
    return "nothing yet — call voice_listen again to keep listening."


def main() -> None:
    try:
        import mcp.server.stdio  # noqa: PLC0415
    except ImportError:
        raise SystemExit(
            "voco-mcp requires the 'mcp' package: uv sync --extra mcp"
        )

    async def run() -> None:
        server = build_server()
        async with mcp.server.stdio.stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    asyncio.run(run())


if __name__ == "__main__":
    main()
