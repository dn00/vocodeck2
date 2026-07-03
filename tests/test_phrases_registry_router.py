"""Phrase table (SPEC §6), registry (SPEC §8.2–8.3), router degraded mode."""

from __future__ import annotations

import asyncio

from voco.core import phrases
from voco.core.registry import Registry
from voco.core.router import Router
from voco.core.turn import RouteDecision


# ---- phrases ---------------------------------------------------------------


def test_hard_commands_match_with_punctuation_and_case():
    assert phrases.match("Stop!", []).kind == "stop"
    assert phrases.match("never mind", []).kind == "stop"
    assert phrases.match("Be quiet.", []).kind == "mute"
    assert phrases.match("what sessions are connected?", []).kind == "sessions"


def test_switch_resolves_fuzzy_call_name():
    cmd = phrases.match("switch to helena", ["Helena", "Marcus"])
    assert cmd.kind == "switch" and cmd.target == "Helena"
    # STT mangling within fuzz range still resolves.
    cmd = phrases.match("talk to elena", ["Helena", "Marcus"])
    assert cmd is not None and cmd.target == "Helena"


def test_open_instructions_never_match():
    names = ["Helena"]
    assert phrases.match("stop the deployment if tests fail", names) is None
    assert phrases.match("refactor the session handler", names) is None
    assert phrases.match("switch to something better than regex", names) is None


# ---- registry ---------------------------------------------------------------


def ident(cwd: str = "/repo/a", host: str = "mac") -> dict:
    return {"host": host, "user": "dn", "cwd": cwd, "harness": "claude"}


def test_register_is_idempotent_by_identity_and_names_are_distinct():
    r = Registry()
    a1 = r.register(ident(), ["say", "listen"])
    a2 = r.register(ident(), ["say", "listen"])
    assert a1.session_id == a2.session_id
    b = r.register(ident(cwd="/repo/b"), ["say"])
    assert b.session_id != a1.session_id
    assert b.call_name != a1.call_name


def test_only_session_auto_activates_and_detach_leaves_none_active():
    r = Registry()
    a = r.register(ident(), ["say", "listen"])
    assert r.active is a
    b = r.register(ident(cwd="/repo/b"), ["say", "listen"])
    assert r.active is a  # no auto-election beyond the first
    r.detach(a.session_id)
    assert r.active is None  # SPEC §5.4 rule 6: announce and wait


def test_dispatch_states_and_queueing():
    r = Registry()
    s = r.register(ident(), ["say", "listen"])
    assert s.state == "idle"
    # Not parked: queues, flagged idle (line-dead earcon case).
    assert r.dispatch("hello", r.mint_turn_id()) == "queued_idle"
    # Parked with live delivery.
    delivered: list[dict] = []
    r.try_deliver = lambda sid, payload: (delivered.append(payload), True)[1]
    immediate = r.on_listen_start(s.session_id)
    # Queued input is delivered on listen start, not parked over.
    assert immediate is not None and immediate["text"] == "hello"
    assert s.state == "working"  # outstanding turn, never goes stale
    # Second listen ends the working turn and parks.
    assert r.on_listen_start(s.session_id) is None
    assert s.state == "parked"
    assert r.dispatch("next", r.mint_turn_id()) == "live"
    assert delivered[0]["text"] == "next"
    assert s.state == "working"


def test_say_records_and_flags_background_digest():
    r = Registry()
    a = r.register(ident(), ["say"])
    b = r.register(ident(cwd="/repo/b"), ["say"])
    assert r.record_say(a.session_id, "active talk", None) is True
    assert r.record_say(b.session_id, "background talk", None) is False
    assert b.unread_digest == 1
    r.switch(b.call_name)
    assert b.unread_digest == 0


# ---- router degraded mode -----------------------------------------------------


def test_router_degraded_mode_phrase_or_forward():
    router = Router(llm=None)
    routed = asyncio.run(router.decide("stop", ["Helena"], {}))
    assert routed.phrase is not None and routed.phrase.kind == "stop"
    routed = asyncio.run(router.decide("please refactor the parser", ["Helena"], {}))
    assert routed.decision is not None and routed.decision.kind == "forward"


def test_router_coerces_bad_gemma_output():
    class BadLlm:
        async def route(self, text: str, grounding: dict) -> RouteDecision | None:
            return RouteDecision(kind="answer", speech="   ")

    router = Router(llm=BadLlm())
    routed = asyncio.run(router.decide("how are you", [], {}))
    assert routed.decision.kind == "forward"

    class SlowLlm:
        async def route(self, text: str, grounding: dict) -> RouteDecision | None:
            await asyncio.sleep(5)
            return RouteDecision(kind="answer", speech="too late")

    router = Router(llm=SlowLlm())
    routed = asyncio.run(router.decide("how are you", [], {}))
    assert routed.decision.kind == "forward"
