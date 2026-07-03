"""Arbitration rules 0–5 (SPEC §5.4)."""

from __future__ import annotations

from voco.core.arbitration import (
    DuplexMode,
    PlaybackItem,
    PlaybackQueue,
    Source,
)


class FakePlayer:
    def __init__(self) -> None:
        self.played: list[PlaybackItem] = []
        self.stops = 0

    def play(self, item: PlaybackItem) -> None:
        self.played.append(item)

    def stop(self) -> None:
        self.stops += 1


def make() -> tuple[PlaybackQueue, FakePlayer, list]:
    player = FakePlayer()
    events: list = []
    q = PlaybackQueue(player, emit=lambda t, p: events.append((t, p)))
    return q, player, events


def ack(ms: int = 200) -> PlaybackItem:
    return PlaybackItem(Source.ACK, "chirp", duration_ms=ms)


def test_rule0_gate_queues_speech_but_allows_short_ack_in_full_duplex():
    q, player, _ = make()
    q.set_gate(True)
    # Real timeline: chirp at VAD close (gated window), gemma speech later.
    q.enqueue(ack())
    assert [i.source for i in player.played] == [Source.ACK]  # exemption
    q.on_item_finished()
    q.enqueue(PlaybackItem(Source.GEMMA, "hello", turn_id="t-1"))
    assert len(player.played) == 1  # gemma still gated
    q.set_gate(False)
    assert [i.source for i in player.played] == [Source.ACK, Source.GEMMA]


def test_rule0_no_ack_exemption_in_half_duplex():
    q, player, _ = make()
    q.set_duplex(DuplexMode.HALF)
    q.set_gate(True)
    q.enqueue(ack())
    assert player.played == []
    q.set_gate(False)
    assert len(player.played) == 1


def test_rule1_barge_in_flushes_playing_and_queued():
    q, player, events = make()
    q.enqueue(PlaybackItem(Source.GEMMA, "a", turn_id="t-1"))
    q.enqueue(PlaybackItem(Source.AGENT, "b", turn_id="t-0"))
    q.barge_in()
    assert player.stops == 1
    q.on_item_finished()  # no-op: nothing playing
    assert len(player.played) == 1  # nothing new started
    assert ("speech.interrupted", {"source": "gemma", "turn_id": "t-1", "reason": "barge-in"}) in events


def test_rule2_current_turn_agent_say_preempts_gemma():
    q, player, _ = make()
    q.note_dispatch("t-7")
    q.enqueue(PlaybackItem(Source.GEMMA, "thinking out loud", turn_id="t-7"))
    assert player.played[-1].source is Source.GEMMA
    q.enqueue(PlaybackItem(Source.AGENT, "real answer", turn_id="t-7"))
    assert player.stops == 1
    assert player.played[-1].source is Source.AGENT


def test_rule2_old_turn_agent_say_queues_behind():
    q, player, _ = make()
    q.note_dispatch("t-9")
    q.enqueue(PlaybackItem(Source.GEMMA, "current turn speech", turn_id="t-9"))
    q.enqueue(PlaybackItem(Source.AGENT, "late say from before", turn_id="t-2"))
    assert player.stops == 0  # no preemption
    assert player.played[-1].source is Source.GEMMA


def test_rule3_gemma_never_plays_after_agent_spoke_that_turn():
    q, player, _ = make()
    q.note_dispatch("t-3")
    q.enqueue(PlaybackItem(Source.AGENT, "answer", turn_id="t-3"))
    q.on_item_finished()
    q.enqueue(PlaybackItem(Source.GEMMA, "stale ack", turn_id="t-3"))
    assert [i.source for i in player.played] == [Source.AGENT]


def test_rule4_fillers_dropped_when_real_speech_exists():
    q, player, _ = make()
    q.enqueue(PlaybackItem(Source.GEMMA, "speaking", turn_id="t-1"))
    q.enqueue(ack())
    q.on_item_finished()
    assert [i.source for i in player.played] == [Source.GEMMA]
    # And queued acks are purged when real speech arrives.
    q.set_gate(True)
    q.set_duplex(DuplexMode.HALF)
    q.enqueue(ack())
    q.enqueue(PlaybackItem(Source.AGENT, "answer", turn_id="t-2"))
    q.set_gate(False)
    assert [i.source for i in player.played] == [Source.GEMMA, Source.AGENT]


def test_rule5_chime_waits_for_idle():
    q, player, _ = make()
    q.enqueue(PlaybackItem(Source.GEMMA, "talking", turn_id="t-1"))
    q.enqueue(PlaybackItem(Source.CHIME, "bg ping"))
    assert player.played[-1].source is Source.GEMMA
    q.on_item_finished()
    assert player.played[-1].source is Source.CHIME
