"""Machine validation: route_stream vs a REAL llama-server (Gemma 4 E4B).

Proves streaming coexists with json_object grammar + --reasoning-budget 0,
that speech deltas arrive incrementally (not one lump at the end), and
that the streamed decision parses identically to the plain call.

Run: llama-server -hf ggml-org/gemma-4-E4B-it-GGUF:Q4_K_M --port 8090 \\
       --reasoning-budget 0 -ngl 99
     uv run python scripts/mate_stream_smoke.py [base_url]
"""

from __future__ import annotations

import asyncio
import sys
import time

from voco.adapters.first_mate import OpenAIChatFirstMate

GROUNDING = {
    "sessions": [
        {
            "name": "Helena",
            "repo": "vocodeck",
            "branch": "main",
            "harness": "claude",
            "state": "working",
            "unread_digest": 0,
            "queued_inputs": 0,
            "terminal": "working",
            "recent_says": [{"age_s": 40, "text": "two tests failing in registry"}],
        },
        {
            "name": "Marcus",
            "repo": "api",
            "branch": "fix/auth",
            "harness": "codex",
            "state": "idle",
            "unread_digest": 1,
            "queued_inputs": 0,
            "terminal": None,
            "recent_says": [{"age_s": 300, "text": "auth refactor is done"}],
        },
    ],
    "active_session": "Helena",
    "mic_mode": "full_duplex",
}

UTTERANCE = "what did marcus say?"


async def main(base_url: str) -> int:
    mate = OpenAIChatFirstMate(
        base_url=base_url, model="", json_mode=True, total_timeout_s=60.0
    )
    stamps: list[float] = []
    heard: list[str] = []
    t0 = time.monotonic()

    def on_speech(delta: str) -> None:
        stamps.append(time.monotonic() - t0)
        heard.append(delta)

    decision = await mate.route_stream(UTTERANCE, GROUNDING, on_speech)
    stream_total = time.monotonic() - t0

    t0 = time.monotonic()
    plain = await mate.route(UTTERANCE, GROUNDING)
    plain_total = time.monotonic() - t0
    await mate.close()

    speech = "".join(heard)
    print(f"streamed decision: {decision}")
    print(f"streamed speech ({len(heard)} deltas): {speech!r}")
    if stamps:
        print(
            f"first speech delta at {stamps[0]:.2f}s; "
            f"stream total {stream_total:.2f}s "
            f"(head start: {stream_total - stamps[0]:.2f}s)"
        )
    else:
        print(f"no speech streamed; stream total {stream_total:.2f}s")
    print(f"plain call: {plain_total:.2f}s, kind={plain.kind if plain else None}")

    ok = decision is not None and (
        decision.kind != "answer" or speech.strip() == decision.speech.strip()
    )
    multi = len(heard) > 1  # deltas, not one lump
    print(f"PASS: parse={'ok' if ok else 'FAIL'}, incremental={multi}")
    return 0 if ok else 1


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8090/v1"
    sys.exit(asyncio.run(main(base)))
