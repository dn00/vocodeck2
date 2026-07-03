"""First-mate model adapter (SPEC §7.3) — any OpenAI-compatible chat endpoint
(llama-server serving Gemma 4 E4B in production).

ROLE: transport only — one /chat/completions call per utterance with the
contract prompt + grounding block; parsing and validation live in
core/first_mate.py. The Router enforces the decision timeout; this client
keeps its own slightly larger socket budget (derived from timeout_ms by
the daemon) so abandoned calls don't leak.

INVARIANTS: any failure returns None (router coerces to forward); the
session is reused across calls (llama-server keeps the prompt cache
warm); streaming never changes what gets parsed — route_stream feeds the
same accumulated text to the same parse_decision as route().
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterable, Callable

import aiohttp

from voco.core.first_mate import SYSTEM_PROMPT, SpeechStream, parse_decision
from voco.core.turn import RouteDecision


class OpenAIChatFirstMate:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        max_tokens: int = 160,
        json_mode: bool = True,
        total_timeout_s: float = 2.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._max_tokens = max_tokens
        self._json_mode = json_mode
        self._total_timeout_s = total_timeout_s
        self._session: aiohttp.ClientSession | None = None

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(
                    total=self._total_timeout_s, sock_connect=0.5
                )
            )
        return self._session

    def _request(self, text: str, grounding: dict, stream: bool) -> tuple[dict, dict]:
        headers = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        "GROUNDING (daemon-observed facts):\n"
                        + json.dumps(grounding, ensure_ascii=False)
                        + "\n\nUSER SAID:\n"
                        + text
                    ),
                },
            ],
            "max_tokens": self._max_tokens,
            "temperature": 0.2,
        }
        if stream:
            body["stream"] = True
        if self._json_mode:
            # llama.cpp grammar-enforces this; steering-only elsewhere.
            body["response_format"] = {"type": "json_object"}
        return body, headers

    async def route(self, text: str, grounding: dict) -> RouteDecision | None:
        body, headers = self._request(text, grounding, stream=False)
        try:
            raw = await self._complete(body, headers)
        except Exception:
            return None  # router coerces to forward (SPEC §7.3)
        roster = [s["name"] for s in grounding.get("sessions", [])]
        return parse_decision(raw, roster)

    async def _complete(self, body: dict, headers: dict) -> str:
        # One retry on connection-level errors: the pool may hold a stale
        # keep-alive (llama-server restarted between utterances).
        for attempt in (0, 1):
            try:
                async with self._ensure_session().post(
                    f"{self._base_url}/chat/completions", json=body, headers=headers
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                return data["choices"][0]["message"]["content"]
            except aiohttp.ClientConnectionError:
                if attempt:
                    raise
        raise RuntimeError("unreachable")

    async def route_stream(
        self,
        text: str,
        grounding: dict,
        on_speech: Callable[[str], None],
    ) -> RouteDecision | None:
        """Streaming variant: on_speech receives decoded speech-text deltas
        as the model emits the JSON speech field; the returned decision is
        parsed from the identical accumulated text route() would have seen."""
        body, headers = self._request(text, grounding, stream=True)
        emitted = False

        def sink(delta: str) -> None:
            nonlocal emitted
            emitted = True
            on_speech(delta)

        try:
            raw = None
            for attempt in (0, 1):
                try:
                    async with self._ensure_session().post(
                        f"{self._base_url}/chat/completions",
                        json=body,
                        headers=headers,
                    ) as resp:
                        resp.raise_for_status()
                        try:
                            raw = await self._consume_sse(resp.content, sink)
                        finally:
                            # We stop reading at [DONE]: a partially
                            # consumed response must never return to the
                            # keep-alive pool — it poisons the NEXT call
                            # ("cannot write to closing transport").
                            resp.close()
                    break
                except aiohttp.ClientConnectionError:
                    # Never retry once speech reached the sink — the user
                    # may have heard it; a replay would double-speak.
                    if attempt or emitted:
                        raise
            assert raw is not None
        except Exception:
            return None  # router coerces to forward (SPEC §7.3)
        roster = [s["name"] for s in grounding.get("sessions", [])]
        return parse_decision(raw, roster)

    @staticmethod
    async def _consume_sse(
        lines: AsyncIterable[bytes], on_speech: Callable[[str], None]
    ) -> str:
        """Accumulate content deltas from an OpenAI-style SSE stream,
        emitting decoded speech deltas along the way. Factored for tests:
        any async iterator of raw lines works."""
        extractor = SpeechStream()
        async for raw_line in lines:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue  # comments / keep-alives
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                delta = json.loads(payload)["choices"][0].get("delta", {})
            except (json.JSONDecodeError, KeyError, IndexError):
                continue  # malformed frame: skip, the full-text parse decides
            content = delta.get("content")
            if not content:
                continue
            new_speech = extractor.feed(content)
            if new_speech:
                try:
                    on_speech(new_speech)
                except Exception:
                    pass  # named fail-silent: a broken sink must not
                    # abort parsing — the decision still matters
        return extractor.buffer

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
