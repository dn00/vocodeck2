"""Agent-side review surface (SPEC-WORKBENCH §4.2) — CLI formatters and
the MCP tool helpers, driven with a stubbed Client (transport is urllib;
the HTTP routes themselves are covered in test_workbench_http.py)."""

from __future__ import annotations

from voco_cli.main import format_review, format_review_item, format_transcript
from voco_mcp.main import _page_push, _review_findings, _review_reply

FINDING = {
    "finding_id": "f-1a",
    "kind": "concern",
    "blocking": True,
    "status": "open",
    "text": "this leaks the fd",
    "anchor": {"file": "src/a.py", "side": "new", "startLine": 3, "endLine": 5},
}
ASK = {"ask_id": "a-9f", "text": "why a retry loop?", "answer": None}


def item(kind: str, payload: dict) -> dict:
    id_key = "finding_id" if kind == "finding" else "ask_id"
    return {"kind": kind, "id": payload[id_key], kind: payload}


# ---- formatters ---------------------------------------------------------------


def test_format_finding_item_names_kind_blocking_and_anchor():
    line = format_review_item(item("finding", FINDING))
    assert line == (
        "[review finding f-1a, concern, blocking src/a.py:3-5] this leaks the fd"
    )


def test_format_single_line_anchor_omits_end():
    f = {**FINDING, "blocking": False, "anchor": {"file": "b.py", "startLine": 7}}
    assert format_review_item(item("finding", f)) == (
        "[review finding f-1a, concern b.py:7] this leaks the fd"
    )


def test_format_ask_item():
    assert format_review_item(item("ask", ASK)) == "[review ask a-9f] why a retry loop?"


def test_format_review_wake_has_header_and_footer():
    text = format_review({"status": "review", "items": [item("ask", ASK)]})
    lines = text.split("\n")
    assert lines[0].startswith("[review]")
    assert "[review ask a-9f]" in lines[1]
    assert "voco review" in lines[-1]  # tells the agent how to respond


def test_review_items_ride_transcript_backlog():
    result = {
        "status": "transcript",
        "text": "and update the docs",
        "queued": [
            {
                "ts": 0,
                "turn_id": "t-1",
                "text": "run tests",
                "origin": "voice",
                "age_s": 0,
            },
            item("finding", FINDING),
        ],
    }
    lines = format_transcript(result).split("\n")
    assert lines[0] == "[queued while working] run tests"
    assert lines[1].startswith("[review finding f-1a")
    assert lines[-1] == "and update the docs"


# ---- MCP tool helpers -----------------------------------------------------------


class StubClient:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.findings_result: dict = {"workspace": "w", "findings": [], "asks": []}

    def findings(self, *, pending: bool = True) -> dict:
        self.calls.append(("findings", pending))
        return self.findings_result

    def finding_status(self, finding_id, status, *, note=None, commit=None) -> dict:
        self.calls.append(("finding_status", finding_id, status, note, commit))
        return {"ok": True}

    def reply(self, item_id, markdown) -> dict:
        self.calls.append(("reply", item_id, markdown))
        return {"ok": True}

    def page_push(self, body) -> dict:
        self.calls.append(("page_push", body))
        return {"ok": True, "page_id": "pg-7", "rev": 2}


def test_review_findings_lists_findings_and_asks():
    c = StubClient()
    c.findings_result = {"workspace": "w", "findings": [FINDING], "asks": [ASK]}
    out = _review_findings(c, False)
    assert "[review finding f-1a" in out
    assert "[review ask a-9f]" in out
    assert ("findings", True) in c.calls  # pending by default


def test_review_findings_empty():
    assert _review_findings(StubClient(), False) == "no pending review items"


def test_review_reply_sets_status_with_note():
    c = StubClient()
    out = _review_reply(c, {"id": "f-1a", "status": "addressed", "note": "renamed"})
    assert ("finding_status", "f-1a", "addressed", "renamed", None) in c.calls
    assert out == "f-1a: status → addressed"


def test_review_reply_answers_ask():
    c = StubClient()
    out = _review_reply(c, {"id": "a-9f", "markdown": "it self-heals"})
    assert ("reply", "a-9f", "it self-heals") in c.calls
    assert out == "a-9f: answered"


def test_review_reply_answer_and_status_together():
    c = StubClient()
    out = _review_reply(
        c, {"id": "f-1a", "markdown": "see note", "status": "addressed"}
    )
    assert out == "f-1a: answered, status → addressed"


def test_review_reply_status_on_ask_is_named_noop():
    out = _review_reply(StubClient(), {"id": "a-9f", "status": "addressed"})
    assert "status ignored" in out


def test_review_reply_with_nothing_says_so():
    assert "nothing to do" in _review_reply(StubClient(), {"id": "f-1a"})


def test_page_push_doc_and_diff_shapes():
    c = StubClient()
    assert "pg-7" in _page_push(c, {"path": "notes.md", "name": "Notes"})
    assert c.calls[-1] == (
        "page_push",
        {"type": "doc", "path": "notes.md", "name": "Notes"},
    )
    _page_push(c, {"diff": {"branch": ""}})
    assert c.calls[-1] == ("page_push", {"type": "diff", "source": {"branch": ""}})
    _page_push(c, {"diff": {"staged": True}})
    assert c.calls[-1] == ("page_push", {"type": "diff", "source": {"staged": True}})
    _page_push(c, {"diff": {"pr": 42}})
    assert c.calls[-1] == ("page_push", {"type": "diff", "source": {"pr": 42}})


def test_page_push_without_args_is_a_hint_not_an_error():
    assert "needs a doc" in _page_push(StubClient(), {})


# ---- identity re-assertion + cache keying (dogfood failure, 2026-07-06) --------


def test_cache_key_distinguishes_same_basename_checkouts():
    from voco_cli.main import Client

    c = Client()
    a = c._cache_path({"host": "h", "cwd": "/a/vocodeck2", "harness": "x"})
    b = c._cache_path({"host": "h", "cwd": "/b/vocodeck2", "harness": "x"})
    assert a != b  # two checkouts, one basename — never one session


def test_session_refreshes_identity_snapshot(monkeypatch, tmp_path):
    from voco_cli import main as cli

    monkeypatch.setattr(cli, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(
        cli,
        "derive_identity",
        lambda: {"host": "h", "cwd": "/now", "harness": "x", "instance": None},
    )
    c = cli.Client()
    monkeypatch.setattr(c, "register", lambda identity=None: {"session_id": "s1"})
    c.session()
    assert c._identity is not None and c._identity["cwd"] == "/now"


def test_workspace_verbs_carry_current_identity(monkeypatch):
    from voco_cli.main import Client

    c = Client()
    c._identity = {"host": "h", "cwd": "/now", "harness": "x"}
    sent: list[tuple] = []

    def fake_request(method, path, body=None, timeout=55.0):
        sent.append((path, body))
        return {}

    monkeypatch.setattr(c, "_request", fake_request)
    monkeypatch.setattr(c, "session", lambda: {"session_id": "s1"})
    c.page_push({"type": "doc", "path": "/x"})
    c.finding_status("f-1", "addressed")
    c.reply("a-1", "done")
    for _path, body in sent:
        assert body["identity"]["cwd"] == "/now"
    c.findings()  # a GET: identity rides the query string instead
    get_path, get_body = sent[-1]
    assert get_body is None and "identity=" in get_path
