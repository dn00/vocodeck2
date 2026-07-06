"""W2 — the unified wake (SPEC-WORKBENCH §4.2/§4.3).

Findings and asks reach agents through the listen park they already hold:
- only `review`-capable sessions see review payloads;
- a parked listen wakes as {status: "review", items};
- items ride `queued` alongside transcripts (voice always wins);
- delivery is at-least-once, idempotent by item id, ledger-authoritative;
- workspace items wake the PRIMARY agent only (election §4.3).

Driven at the daemon level (real Daemon, no audio, stubbed try_deliver):
the wiring under test is registry <-> workspace store <-> bus <-> daemon.
"""

from __future__ import annotations

import pytest

from voco.core.workspace import WorkspaceStore
from voco.daemon import Daemon
from voco.server.workbench import handle_workbench_command


class Delivery:
    """try_deliver stub: records payloads; scripted to accept or refuse."""

    def __init__(self, accept: bool = True) -> None:
        self.accept = accept
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, session_id: str, payload: dict) -> bool:
        self.calls.append((session_id, payload))
        return self.accept


@pytest.fixture
def daemon() -> Daemon:
    d = Daemon({}, no_audio=True)
    d._wire_review_wake()  # run() does this; tests wire it explicitly
    return d


def ident(cwd: str = "/repo/a", host: str = "mac") -> dict:
    return {
        "host": host,
        "user": "dn",
        "cwd": cwd,
        "worktree": cwd,
        "repo": cwd.rsplit("/", 1)[-1],
        "branch": "main",
        "harness": "claude",
    }


def attach(d: Daemon, cwd: str = "/repo/a", caps: list[str] | None = None):
    """Register a session AND give its workspace a diff page to anchor
    findings to. Returns (session, workspace, page)."""
    s = d.registry.register(ident(cwd), caps or ["say", "listen", "review"])
    ws = d.workspaces.resolve(s.identity)
    page = d.workspaces.upsert_diff(
        ws, ref="branch:main", title="branch:main", files=[], source=None
    )
    return s, ws, page


def add_finding(d: Daemon, ws, page, text: str = "rename this", **kw):
    return d.workspaces.add_finding(
        ws.key,
        page_id=page.page_id,
        anchor={"file": "a.py", "side": "new", "startLine": 3, "endLine": 3},
        text=text,
        **kw,
    )


# ---- wake on finding ---------------------------------------------------------


def test_finding_wakes_parked_review_session(daemon):
    s, ws, page = attach(daemon)
    delivery = Delivery()
    daemon.registry.try_deliver = delivery
    assert daemon.registry.on_listen_start(s.session_id) is None  # parked

    f = add_finding(daemon, ws, page)

    assert len(delivery.calls) == 1
    sid, payload = delivery.calls[0]
    assert sid == s.session_id
    assert payload["status"] == "review"
    (item,) = payload["items"]
    assert item["kind"] == "finding"
    assert item["id"] == f.finding_id
    assert item["workspace"] == ws.key
    assert daemon.registry.get(s.session_id).parked is False  # woke it


def test_ask_wakes_parked_review_session(daemon):
    s, ws, _page = attach(daemon)
    delivery = Delivery()
    daemon.registry.try_deliver = delivery
    daemon.registry.on_listen_start(s.session_id)

    handle_workbench_command(
        daemon.workspaces,
        "ask.create",
        {"workspace": ws.key, "text": "why the retry loop?"},
        data_dir=None,
    )

    assert len(delivery.calls) == 1
    _, payload = delivery.calls[0]
    assert payload["status"] == "review"
    assert payload["items"][0]["kind"] == "ask"


def test_non_review_session_never_sees_review_items(daemon):
    s, ws, page = attach(daemon, caps=["say", "listen"])
    delivery = Delivery()
    daemon.registry.try_deliver = delivery
    daemon.registry.on_listen_start(s.session_id)

    add_finding(daemon, ws, page)

    assert delivery.calls == []  # not woken
    # ...and a fresh listen parks instead of returning items.
    assert daemon.registry.on_listen_start(s.session_id) is None


def test_unparked_session_gets_items_on_next_listen(daemon):
    """Not parked at finding time (mid-work): the wake is a no-op and the
    items arrive on the NEXT listen — the at-least-once path."""
    s, ws, page = attach(daemon)
    delivery = Delivery()
    daemon.registry.try_deliver = delivery

    f = add_finding(daemon, ws, page)
    assert delivery.calls == []  # nothing parked, nothing delivered

    payload = daemon.registry.on_listen_start(s.session_id)
    assert payload["status"] == "review"
    assert payload["items"][0]["id"] == f.finding_id


# ---- at-least-once redelivery ------------------------------------------------


def test_redelivery_until_status_leaves_open(daemon):
    """An agent that crashes between wake and action sees the item again;
    acting on it (finding_status) stops redelivery."""
    s, ws, page = attach(daemon)
    f = add_finding(daemon, ws, page)

    for _ in range(2):  # every listen redelivers while it stays open
        payload = daemon.registry.on_listen_start(s.session_id)
        assert payload["status"] == "review"
        assert payload["items"][0]["id"] == f.finding_id

    daemon.workspaces.set_finding_status(
        ws.key, f.finding_id, "addressed", note="renamed", agent=True
    )
    assert daemon.registry.on_listen_start(s.session_id) is None  # parks


def test_missed_wake_redelivers_on_next_listen(daemon):
    """try_deliver says the poll vanished (timeout race): the session is
    still parked, and the items ride the next listen."""
    s, ws, page = attach(daemon)
    delivery = Delivery(accept=False)
    daemon.registry.try_deliver = delivery
    daemon.registry.on_listen_start(s.session_id)

    f = add_finding(daemon, ws, page)
    assert len(delivery.calls) == 1  # attempted...
    assert daemon.registry.get(s.session_id).parked is True  # ...but kept parked

    payload = daemon.registry.on_listen_start(s.session_id)
    assert payload["items"][0]["id"] == f.finding_id


def test_answered_ask_stops_redelivery(daemon):
    s, ws, _page = attach(daemon)
    a = daemon.workspaces.add_ask(ws.key, text="ship it?")
    payload = daemon.registry.on_listen_start(s.session_id)
    assert payload["items"][0]["id"] == a.ask_id

    daemon.workspaces.answer_ask(ws.key, a.ask_id, "yes — after the tests")
    assert daemon.registry.on_listen_start(s.session_id) is None


def test_answering_question_finding_addresses_it(daemon):
    """A question-kind finding is also an ask (§4.2): the reply IS the
    round-trip, so redelivery must converge without a separate
    finding_status call."""
    s, ws, page = attach(daemon)
    f = add_finding(daemon, ws, page, text="why not a set?", kind="question")
    assert daemon.registry.on_listen_start(s.session_id) is not None

    daemon.workspaces.answer_finding(ws.key, f.finding_id, "dicts keep order")

    assert daemon.workspaces.get(ws.key).findings[f.finding_id].status == "addressed"
    assert daemon.registry.on_listen_start(s.session_id) is None


def test_answering_concern_finding_keeps_it_open(daemon):
    """Only question-kind findings auto-address on reply: a concern still
    needs its explicit finding_status round-trip."""
    s, ws, page = attach(daemon)
    f = add_finding(daemon, ws, page, text="this leaks", kind="concern")
    daemon.workspaces.answer_finding(ws.key, f.finding_id, "context attached")

    assert daemon.workspaces.get(ws.key).findings[f.finding_id].status == "open"
    assert daemon.registry.on_listen_start(s.session_id) is not None


# ---- voice always wins (ride-along) -------------------------------------------


def test_review_items_ride_queued_behind_transcripts(daemon):
    s, ws, page = attach(daemon)
    f = add_finding(daemon, ws, page)
    daemon.registry.dispatch("run the tests", "t-9", target=s)  # queues (not parked)

    payload = daemon.registry.on_listen_start(s.session_id)
    assert payload["status"] == "transcript"
    assert payload["text"] == "run the tests"
    kinds = [q.get("kind") for q in payload["queued"]]
    assert kinds == ["finding"]
    assert payload["queued"][0]["id"] == f.finding_id


def test_live_dispatch_carries_review_items_in_queued(daemon):
    """§4.2: pending items ALWAYS ride `queued` — including on a live
    voice delivery, not just on listen."""
    s, ws, page = attach(daemon)
    f = add_finding(daemon, ws, page)
    delivery = Delivery()
    daemon.registry.try_deliver = delivery
    daemon.registry.on_listen_start(s.session_id)  # park AFTER the finding

    # Parked with pending items never happens via on_listen_start (it
    # returns them immediately) — but a wake_review refusal race can leave
    # it. Simulate: park bypassing the immediate return.
    daemon.registry.get(s.session_id).parked = True
    daemon.registry.dispatch("also fix the docs", "t-3", target=s)

    _, payload = delivery.calls[-1]
    assert payload["status"] == "transcript"
    assert [q.get("id") for q in payload["queued"]] == [f.finding_id]


# ---- primary election (§4.3) ---------------------------------------------------


def test_active_session_in_workspace_is_primary(daemon):
    s1, ws, page = attach(daemon)  # first registration auto-activates
    s2 = daemon.registry.register(
        {**ident(), "instance": "pane-2"}, ["say", "listen", "review"]
    )
    delivery = Delivery()
    daemon.registry.try_deliver = delivery
    daemon.registry.on_listen_start(s1.session_id)
    daemon.registry.on_listen_start(s2.session_id)

    add_finding(daemon, ws, page)

    woken = {sid for sid, _ in delivery.calls}
    assert woken == {s1.session_id}  # active wins; the other reads the ledger


def test_most_recent_review_session_is_primary_when_active_elsewhere(daemon):
    now = [1000.0]
    daemon.registry._now = lambda: now[0]
    s1, ws, page = attach(daemon)
    now[0] = 2000.0
    s2 = daemon.registry.register(
        {**ident(), "instance": "pane-2"}, ["say", "listen", "review"]
    )
    # Active session lives in ANOTHER workspace.
    other, _, _ = attach(daemon, cwd="/repo/b")
    daemon.registry.switch(other.call_name)

    delivery = Delivery()
    daemon.registry.try_deliver = delivery
    now[0] = 3000.0
    daemon.registry.on_listen_start(s1.session_id)
    now[0] = 4000.0
    daemon.registry.on_listen_start(s2.session_id)  # parked most recently

    add_finding(daemon, ws, page)

    woken = {sid for sid, _ in delivery.calls}
    assert woken == {s2.session_id}  # most recently seen review agent


def test_non_primary_listen_stays_parked(daemon):
    s1, ws, page = attach(daemon)
    s2 = daemon.registry.register(
        {**ident(), "instance": "pane-2"}, ["say", "listen", "review"]
    )
    add_finding(daemon, ws, page)

    assert daemon.registry.on_listen_start(s2.session_id) is None  # not primary
    assert daemon.registry.on_listen_start(s1.session_id) is not None


def test_election_never_creates_workspaces(daemon):
    """home_of is a read: electing across sessions whose workspaces don't
    exist yet must not mint sessionspaces (resolve would)."""
    daemon.registry.register(
        {"host": "mac", "cwd": "/tmp/loose", "harness": "codex"},
        ["say", "listen", "review"],
    )
    _s, ws, page = attach(daemon)
    add_finding(daemon, ws, page)
    keys = {w.key for w in daemon.workspaces.all()}
    assert "mac:/tmp/loose" not in keys


# ---- durability -----------------------------------------------------------------


def test_asks_survive_dump_restore(daemon):
    _s, ws, _page = attach(daemon)
    a = daemon.workspaces.add_ask(ws.key, text="which branch?", context={"f": "x"})
    answered = daemon.workspaces.add_ask(ws.key, text="done?")
    daemon.workspaces.answer_ask(ws.key, answered.ask_id, "yes")

    dump = daemon.workspaces.dump_workspace(ws)
    fresh = WorkspaceStore()
    restored = fresh.restore_workspace(dump)

    assert restored is not None
    assert restored.asks[a.ask_id].answer is None
    assert restored.asks[a.ask_id].context == {"f": "x"}
    assert restored.asks[answered.ask_id].answer == "yes"
    # The unanswered ask is still pending after a daemon restart.
    pending = [i for i in restored.pending_review() if i["kind"] == "ask"]
    assert [i["id"] for i in pending] == [a.ask_id]
