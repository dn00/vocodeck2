"""Mate speech streaming: extractor, SSE consumption, router + channel."""

from __future__ import annotations

import asyncio
import json

from voco.adapters.first_mate import OpenAIChatFirstMate
from voco.core.first_mate import SpeechStream, extract_speech_prefix
from voco.core.router import Router
from voco.core.turn import RouteDecision
from voco.voice_loop import MateSpeechChannel

# ---- pure extractor ---------------------------------------------------------


def test_extract_speech_prefix_partial_and_complete():
    assert extract_speech_prefix('{"route": "answer"') == ("", False)
    buf = '{"route": "answer", "speech": "On it'
    assert extract_speech_prefix(buf) == ("On it", False)
    buf += '. Sending now.", "target": null}'
    assert extract_speech_prefix(buf) == ("On it. Sending now.", True)


def test_extract_speech_decodes_escapes_and_waits_on_split_escape():
    buf = '{"speech": "line one\\nline'
    assert extract_speech_prefix(buf) == ("line one\nline", False)
    # Escape split across deltas: hold until it completes.
    assert extract_speech_prefix('{"speech": "wait\\')[0] == "wait"
    assert extract_speech_prefix('{"speech": "q: \\"hi\\""') == ('q: "hi"', True)
    assert extract_speech_prefix('{"speech": "u\\u00e9!"')[0] == "ué!"


def test_speech_stream_emits_only_new_text():
    s = SpeechStream()
    assert s.feed('{"route": "ack_forward", "spee') == ""
    assert s.feed('ch": "Send') == "Send"
    assert s.feed("ing that") == "ing that"
    assert s.feed(' over.", "target": "Marcus"}') == " over."
    assert s.done is True
    assert json.loads(s.buffer)["target"] == "Marcus"  # buffer is the full text


# ---- SSE consumption --------------------------------------------------------


def sse_lines(*payloads: str):
    async def gen():
        yield b": keep-alive\n"
        for p in payloads:
            yield f"data: {p}\n".encode()
        yield b"data: [DONE]\n"

    return gen()


def delta(content: str) -> str:
    return json.dumps({"choices": [{"delta": {"content": content}}]})


async def test_consume_sse_accumulates_and_streams_speech():
    heard: list[str] = []
    full = await OpenAIChatFirstMate._consume_sse(
        sse_lines(
            json.dumps({"choices": [{"delta": {"role": "assistant"}}]}),
            delta('{"route": "answer", "speech": "Helena said '),
            delta('tests pass, a minute ago."'),
            "not json at all",  # malformed frame: skipped
            delta(', "target": null, "action": null}'),
        ),
        heard.append,
    )
    assert "".join(heard) == "Helena said tests pass, a minute ago."
    assert json.loads(full)["route"] == "answer"


# ---- connection hygiene (regression: pool poisoning after SSE) ---------------


async def test_plain_call_survives_after_stream_on_same_session():
    """Breaking out of an SSE stream at [DONE] must not poison the
    keep-alive pool for the NEXT plain call (live-smoke find: second call
    died with 'cannot write to closing transport')."""
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    completion = '{"route": "answer", "speech": "Hi there.", "target": null}'

    async def chat(request: web.Request) -> web.StreamResponse:
        body = await request.json()
        if body.get("stream"):
            resp = web.StreamResponse()
            resp.content_type = "text/event-stream"
            await resp.prepare(request)
            await resp.write(f"data: {delta(completion)}\n\n".encode())
            await resp.write(b"data: [DONE]\n\n")
            await resp.write_eof()
            return resp
        return web.json_response({"choices": [{"message": {"content": completion}}]})

    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat)
    server = TestServer(app)
    await server.start_server()
    try:
        mate = OpenAIChatFirstMate(
            base_url=f"http://127.0.0.1:{server.port}/v1", model="", json_mode=False
        )
        heard: list[str] = []
        streamed = await mate.route_stream("hi", {"sessions": []}, heard.append)
        assert streamed is not None and "".join(heard) == "Hi there."
        plain = await mate.route("hi", {"sessions": []})  # same session, next call
        assert plain is not None and plain.kind == "answer"
        await mate.close()
    finally:
        await server.close()


# ---- router streaming path --------------------------------------------------


class StreamingMate:
    def __init__(self, fail_slow: bool = False) -> None:
        self.fail_slow = fail_slow

    async def route(self, text: str, grounding: dict) -> RouteDecision | None:
        raise AssertionError("streaming path must be preferred when sink given")

    async def route_stream(self, text, grounding, on_speech):
        if self.fail_slow:
            await asyncio.sleep(5)
        on_speech("On it. ")
        on_speech("Sending.")
        return RouteDecision(kind="ack_forward", speech="On it. Sending.")


async def test_router_prefers_stream_and_times_out_to_forward():
    heard: list[str] = []
    router = Router(first_mate=StreamingMate(), timeout_s=1.0)
    routed = await router.decide("do the thing", ["Marcus"], {}, heard.append)
    assert routed.decision.kind == "ack_forward"
    assert heard == ["On it. ", "Sending."]

    router = Router(first_mate=StreamingMate(fail_slow=True), timeout_s=0.05)
    routed = await router.decide("tell Marcus to go", ["Marcus"], {}, heard.append)
    assert routed.decision.kind == "forward"
    assert routed.decision.target == "Marcus"  # misroute guard still applies


# ---- speech channel ---------------------------------------------------------


class FakeTts:
    def __init__(self) -> None:
        self.synthesized: list[str] = []

    def stream(self, text: str):
        self.synthesized.append(text)

        async def gen():
            yield f"pcm:{text}".encode()

        return gen()


class FakeQueue:
    def __init__(self) -> None:
        self.items = []

    def enqueue(self, item) -> None:
        self.items.append(item)


class FakeVoice:
    def __init__(self) -> None:
        self.tts = FakeTts()
        self.queue = FakeQueue()


async def drain(agen) -> list[bytes]:
    return [chunk async for chunk in agen]


async def test_channel_streams_sentences_into_one_item():
    voice = FakeVoice()
    ch = MateSpeechChannel(voice)
    ch.push("On it. Send")
    assert len(voice.queue.items) == 1  # first full sentence -> one item
    ch.push("ing that over")
    assert ch.finish() is True  # flushes the tail
    chunks = await drain(voice.queue.items[0].content)
    assert voice.tts.synthesized == ["On it.", "Sending that over"]
    assert chunks == [b"pcm:On it.", b"pcm:Sending that over"]


async def test_channel_cancel_before_any_sentence_leaves_no_trace():
    voice = FakeVoice()
    ch = MateSpeechChannel(voice)
    ch.push("Sending th")  # no boundary yet
    ch.cancel()
    assert voice.queue.items == [] and ch.consumed is False
    ch.push("more after close is ignored. ")
    assert voice.queue.items == []


async def test_channel_empty_finish_reports_not_consumed():
    voice = FakeVoice()
    ch = MateSpeechChannel(voice)
    assert ch.finish() is False
    assert voice.queue.items == []
