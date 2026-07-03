"""First-mate contract: grounding, parsing, coercion, actions (SPEC §7)."""

from __future__ import annotations

import json

from voco.core.first_mate import (
    build_grounding,
    execute_action,
    parse_decision,
)
from voco.core.registry import Registry

ROSTER = ["Helena", "Marcus"]


def ident(cwd: str = "/repo/a") -> dict:
    return {
        "host": "mac",
        "user": "dn",
        "cwd": cwd,
        "harness": "claude",
        "repo": cwd.split("/")[-1],
        "branch": "main",
    }


# ---- grounding -----------------------------------------------------------


def test_grounding_contains_only_observed_facts_with_ages():
    r = Registry(now=lambda: 100.0)
    a = r.register(ident("/repo/a"), ["say", "listen"])
    b = r.register(ident("/repo/b"), ["say"])
    r.record_say(b.session_id, "two tests failed", None)
    g = build_grounding(r, "full_duplex", now=160.0)
    assert g["active_session"] == a.call_name
    assert g["mic_mode"] == "full_duplex"
    by_name = {s["name"]: s for s in g["sessions"]}
    bg = by_name[b.call_name]
    assert bg["recent_says"] == [{"age_s": 60, "text": "two tests failed"}]
    assert bg["repo"] == "b" and bg["branch"] == "main"
    assert bg["unread_digest"] == 1


# ---- parsing / coercion -------------------------------------------------


def test_parse_clean_json_and_json_wrapped_in_prose():
    raw = (
        '{"route": "ack_forward", "speech": "Sending that over.",'
        ' "target": null, "action": null}'
    )
    d = parse_decision(raw, ROSTER)
    assert d.kind == "ack_forward" and d.speech == "Sending that over."
    wrapped = f"Sure! Here is the JSON:\n```json\n{raw}\n```"
    d = parse_decision(wrapped, ROSTER)
    assert d is not None and d.kind == "ack_forward"


def test_parse_rejects_garbage_and_bad_routes():
    assert parse_decision("I think you should refactor.", ROSTER) is None
    assert parse_decision('{"route": "do_it_myself", "speech": "hi"}', ROSTER) is None
    assert parse_decision("{broken json", ROSTER) is None


def test_targeted_forward_resolves_fuzzy_and_drops_unknown():
    raw = json.dumps({"route": "forward", "speech": "", "target": "helena"})
    assert parse_decision(raw, ROSTER).target == "Helena"
    raw = json.dumps({"route": "forward", "speech": "", "target": "Zorblax"})
    assert parse_decision(raw, ROSTER).target is None  # → active session


def test_action_validation_drops_unknown_and_malformed():
    good = json.dumps(
        {
            "route": "answer",
            "speech": "Switching.",
            "action": {"type": "switch_session", "target": "marcus"},
        }
    )
    d = parse_decision(good, ROSTER)
    assert d.action == {"type": "switch_session", "target": "Marcus"}
    bad_type = json.dumps(
        {"route": "answer", "speech": "ok", "action": {"type": "launch_missiles"}}
    )
    assert parse_decision(bad_type, ROSTER).action is None
    bad_target = json.dumps(
        {
            "route": "answer",
            "speech": "ok",
            "action": {"type": "switch_session", "target": "nobody"},
        }
    )
    assert parse_decision(bad_target, ROSTER).action is None
    bad_mode = json.dumps(
        {
            "route": "answer",
            "speech": "ok",
            "action": {"type": "mic_mode", "mode": "loud"},
        }
    )
    assert parse_decision(bad_mode, ROSTER).action is None


# ---- execution -------------------------------------------------------------


def test_execute_switch_and_mic_actions():
    r = Registry()
    r.register(ident("/repo/a"), ["say"])
    b = r.register(ident("/repo/b"), ["say"])
    mic_calls: list[str] = []
    mute_calls: list[bool] = []
    execute_action(
        {"type": "switch_session", "target": b.call_name},
        r,
        set_mic=mic_calls.append,
        set_muted=mute_calls.append,
    )
    assert r.active is b
    execute_action(
        {"type": "mic_mode", "mode": "half_duplex"},
        r,
        mic_calls.append,
        mute_calls.append,
    )
    assert mic_calls == ["half_duplex"]
    execute_action({"type": "mute"}, r, mic_calls.append, mute_calls.append)
    execute_action({"type": "unmute"}, r, mic_calls.append, mute_calls.append)
    assert mute_calls == [True, False]
    b.unread_digest = 3
    execute_action(
        {"type": "read_digest", "target": None}, r, mic_calls.append, mute_calls.append
    )
    assert b.unread_digest == 0  # None target = active session


def test_fallback_target_prefix_known_names_only():
    from voco.core.first_mate import fallback_target

    roster = ["Helena", "Marcus"]
    assert fallback_target("tell marcus to run the tests", roster) == "Marcus"
    assert fallback_target("Ask Helena about the diff", roster) == "Helena"
    # STT misspelling still resolves (conservative fuzz, same as switch).
    assert fallback_target("tell Markus to go ahead", roster) == "Marcus"
    # Not a leading forward verb -> no guess.
    assert fallback_target("can you tell Marcus something", roster) is None
    # Unknown name -> no guess (active session is the honest default).
    assert fallback_target("tell Bob to stop", roster) is None
