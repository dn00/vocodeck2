"""First-mate model adapter (SPEC §7.3) — any OpenAI-compatible chat endpoint
(llama-server serving Gemma 4 E4B in production).

ROLE: transport only — one /chat/completions call per utterance with the
contract prompt + grounding block; parsing and validation live in
core/first_mate.py. The Router enforces the 800ms
decision timeout; this client keeps its own slightly larger socket budget
so abandoned calls don't leak.

INVARIANTS: any failure returns None (router coerces to forward); the
session is reused across calls (llama-server keeps the prompt cache warm).
"""

from __future__ import annotations

import json

import aiohttp

from voco.core.first_mate import SYSTEM_PROMPT, parse_decision
from voco.core.turn import RouteDecision


class OpenAIChatFirstMate:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        max_tokens: int = 160,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._max_tokens = max_tokens
        self._session: aiohttp.ClientSession | None = None

    async def route(self, text: str, grounding: dict) -> RouteDecision | None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=2.0, sock_connect=0.5)
            )
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
        try:
            async with self._session.post(
                f"{self._base_url}/chat/completions", json=body, headers=headers
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
            raw = data["choices"][0]["message"]["content"]
        except Exception:
            return None  # router coerces to forward (SPEC §7.3)
        roster = [s["name"] for s in grounding.get("sessions", [])]
        return parse_decision(raw, roster)

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
