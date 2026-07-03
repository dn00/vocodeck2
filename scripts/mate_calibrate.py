"""First-mate contract calibration harness (SPEC §7) — no ears required.

Runs an utterance suite through the REAL adapter (OpenAIChatFirstMate)
against a live llama-server, with a synthetic-but-realistic grounding
block, and scores what actually matters:

- parse rate: model output survives parse_decision (JSON discipline)
- route accuracy: answer/forward/ack_forward vs expectation
- target accuracy: "tell Helena ..." resolves the right session
- action accuracy: switch/mute emitted when asked, never otherwise
- authority violations: banned patterns in speech (promises, work claims)
- latency: p50/p95 vs the 800ms router timeout

Run:  uv run python scripts/mate_calibrate.py [--base-url http://127.0.0.1:8089/v1]
Exit code 0 always (calibration report, not a gate); numbers land in
BUILD.md at calibration time.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import statistics
import time

from voco.adapters.first_mate import OpenAIChatFirstMate
from voco.core.first_mate import parse_decision

GROUNDING = {
    "sessions": [
        {
            "name": "Helena",
            "repo": "vocodeck2",
            "branch": "mis/142-tee-attach",
            "harness": "claude",
            "state": "working",
            "unread_digest": 0,
            "recent_says": [
                {"age_s": 65, "text": "Two tests failed in the bridge suite."},
                {"age_s": 20, "text": "Retrying with the fixed fixture now."},
            ],
        },
        {
            "name": "Marcus",
            "repo": "pluely",
            "branch": "main",
            "harness": "codex",
            "state": "parked",
            "unread_digest": 2,
            "recent_says": [
                {"age_s": 300, "text": "Refactor branch is pushed and green."}
            ],
        },
    ],
    "active_session": "Helena",
    "mic_mode": "full_duplex",
}

# (utterance, expected_routes, expected_target, expected_action_type)
SUITE: list[tuple[str, set[str], str | None, str | None]] = [
    # Work → must forward (ack_forward preferred, forward acceptable)
    (
        "refactor the session registry to use slots",
        {"ack_forward", "forward"},
        None,
        None,
    ),
    (
        "run the full test suite and fix whatever breaks",
        {"ack_forward", "forward"},
        None,
        None,
    ),
    ("why is the bridge test flaky", {"ack_forward", "forward"}, None, None),
    (
        "add a retry to the tunnel reconnect logic",
        {"ack_forward", "forward"},
        None,
        None,
    ),
    # Targeted forwards
    (
        "tell Marcus to rebase his branch on main",
        {"ack_forward", "forward"},
        "Marcus",
        None,
    ),
    ("ask helena to commit what she has", {"ack_forward", "forward"}, "Helena", None),
    # Loop/status questions → answer locally (attributed)
    ("what did Helena say", {"answer"}, None, None),
    ("what is Marcus working on", {"answer"}, None, None),
    ("how long has helena been working", {"answer"}, None, None),
    ("did marcus finish the refactor", {"answer"}, None, None),
    # Social → answer locally
    ("good morning voco", {"answer"}, None, None),
    ("thanks, that was fast", {"answer"}, None, None),
    # Actions
    ("switch over to marcus please", {"answer", "ack_forward"}, None, "switch_session"),
    ("go half duplex, I'm on speakers", {"answer", "ack_forward"}, None, "mic_mode"),
    # Adversarial: work questions that LOOK answerable — must forward
    ("is the registry thread safe", {"ack_forward", "forward"}, None, None),
    ("which file has the turn machine in it", {"ack_forward", "forward"}, None, None),
]

# Authority partition violations (SPEC §7.1) — crude but effective probes.
BANNED_IN_SPEECH = [
    (re.compile(r"\bon it\b.*\b(refactor|fix|implement|run)", re.I), "outcome promise"),
    (
        re.compile(r"\b(the code|the bug|the test)s? (is|are|was|were)\b", re.I),
        "work claim",
    ),
    (
        re.compile(r"\bI('ll| will) (fix|refactor|implement|run)\b", re.I),
        "doing work itself",
    ),
]


async def wait_ready(base_url: str, timeout_s: float = 1200.0) -> None:
    import aiohttp

    deadline = time.monotonic() + timeout_s
    async with aiohttp.ClientSession() as s:
        while time.monotonic() < deadline:
            try:
                async with s.get(base_url.replace("/v1", "/health")) as resp:
                    if resp.status == 200:
                        return
            except Exception:
                pass
            await asyncio.sleep(5)
    raise TimeoutError("llama-server never became ready")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8089/v1")
    parser.add_argument("--model", default="gemma-4-e4b-it")
    args = parser.parse_args()

    print("waiting for llama-server ...")
    await wait_ready(args.base_url)
    mate = OpenAIChatFirstMate(base_url=args.base_url, model=args.model)
    # Calibration ignores the router's 800ms guillotine to measure true
    # latency; production keeps the timeout.
    mate._session = None

    roster = [s["name"] for s in GROUNDING["sessions"]]
    parsed = 0
    route_ok = 0
    target_ok = 0
    action_ok = 0
    violations: list[tuple[str, str]] = []
    latencies: list[float] = []
    failures: list[str] = []

    for text, want_routes, want_target, want_action in SUITE:
        t0 = time.perf_counter()
        raw = await mate_raw(mate, text)
        latencies.append(time.perf_counter() - t0)
        decision = parse_decision(raw or "", roster)
        if decision is None:
            failures.append(f"UNPARSED  {text!r} -> {raw!r}")
            continue
        parsed += 1
        if decision.kind in want_routes:
            route_ok += 1
        else:
            failures.append(
                f"ROUTE     {text!r} -> {decision.kind} (want {want_routes})"
            )
        if want_target is not None:
            if decision.target == want_target:
                target_ok += 1
            else:
                failures.append(f"TARGET    {text!r} -> {decision.target}")
        got_action = (decision.action or {}).get("type")
        if want_action is not None:
            if got_action == want_action:
                action_ok += 1
            else:
                failures.append(f"ACTION    {text!r} -> {got_action}")
        elif got_action is not None:
            failures.append(f"SPURIOUS  {text!r} -> action {got_action}")
        for pattern, label in BANNED_IN_SPEECH:
            if decision.speech and pattern.search(decision.speech):
                violations.append((text, f"{label}: {decision.speech!r}"))

    n = len(SUITE)
    n_targets = sum(1 for c in SUITE if c[2] is not None)
    n_actions = sum(1 for c in SUITE if c[3] is not None)
    lat_ms = sorted(x * 1000 for x in latencies)
    print(f"\nparse rate     {parsed}/{n}")
    print(f"route accuracy {route_ok}/{n}")
    print(f"target         {target_ok}/{n_targets}")
    print(f"actions        {action_ok}/{n_actions}")
    print(
        f"latency        p50 {statistics.median(lat_ms):.0f}ms"
        f"  p95 {lat_ms[int(0.95 * (len(lat_ms) - 1))]:.0f}ms"
        f"  (router timeout: 800ms)"
    )
    if violations:
        print(f"\nAUTHORITY VIOLATIONS ({len(violations)}):")
        for text, v in violations:
            print(f"  {text!r}: {v}")
    if failures:
        print(f"\nFAILURES ({len(failures)}):")
        for f in failures:
            print(f"  {f}")


async def mate_raw(mate: OpenAIChatFirstMate, text: str) -> str | None:
    """Call the adapter's transport but capture the raw model text."""
    import json as _json

    import aiohttp

    from voco.core.first_mate import SYSTEM_PROMPT

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=60)
    ) as session, session.post(
        f"{mate._base_url}/chat/completions",
        json={
            "model": mate._model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        "GROUNDING (daemon-observed facts):\n"
                        + _json.dumps(GROUNDING, ensure_ascii=False)
                        + "\n\nUSER SAID:\n"
                        + text
                    ),
                },
            ],
            "max_tokens": 160,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        },
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()
    return data["choices"][0]["message"]["content"]


if __name__ == "__main__":
    asyncio.run(main())
