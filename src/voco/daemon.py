"""voco-d — composition root (SPEC §2, §11).

ROLE: load config, own the decision surface (route/phrase/action/dispatch),
run the bridge server, and host the optional VoiceLoop (the audio shell).
All policy lives in core/; all audio lives in voice_loop.py; this module
only composes and adjudicates.

INVARIANTS: --no-audio runs bridge+core only (headless bring-up, CI); a
failed VoiceLoop construction degrades to headless with a daemon.error,
never a crash (fail-silent toward agents, loud on the event bus).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from voco.voice_loop import VoiceLoop

from voco.core.arbitration import DuplexMode
from voco.core.events import EventBus
from voco.core.first_mate import build_grounding, execute_action
from voco.core.phrases import PhraseCommand
from voco.core.registry import Registry
from voco.core.router import Routed, Router
from voco.core.turn import RouteDecision
from voco.server.http import BridgeServer, run_server

DEFAULT_CONFIG = Path.home() / ".config" / "voco" / "config.toml"


def load_config(path: Path | None) -> dict[str, Any]:
    p = path or DEFAULT_CONFIG
    if p.exists():
        return tomllib.loads(p.read_text())
    return {}


class Daemon:
    def __init__(self, cfg: dict[str, Any], no_audio: bool = False) -> None:
        self.cfg = cfg
        self.no_audio = no_audio
        self.bus = EventBus()
        self.registry = Registry(emit=self.bus.emit)
        mate = None
        mate_cfg = cfg.get("first_mate")
        if mate_cfg:
            from voco.adapters.first_mate import OpenAIChatFirstMate

            mate = OpenAIChatFirstMate(
                base_url=mate_cfg["base_url"],
                model=mate_cfg.get("model", ""),
                api_key=mate_cfg.get("api_key"),
            )
        self.router = Router(first_mate=mate)
        self.bridge = BridgeServer(
            self.registry,
            self.bus,
            token=cfg.get("bridge", {}).get("token"),
            on_control=self._control,
        )
        self.voice: VoiceLoop | None = None

    # ---- VoiceHost port (decisions stay here; audio reacts in VoiceLoop) ----

    async def route(self, text: str) -> Routed:
        """Decide + execute any first-mate action (loop-domain, immediate)."""
        grounding = build_grounding(
            self.registry,
            self.voice.duplex.value if self.voice else "headless",
            time.time(),
        )
        routed = await self.router.decide(text, self.registry.call_names(), grounding)
        if routed.decision is not None and routed.decision.action is not None:
            execute_action(
                routed.decision.action,
                self.registry,
                set_mic=self._set_duplex,
                set_muted=self._set_muted,
            )
        return routed

    def run_phrase(self, cmd: PhraseCommand) -> None:
        if cmd.kind == "stop":
            if self.voice is not None:
                self.voice.barge_in()
        elif cmd.kind == "switch" and cmd.target:
            self.registry.switch(cmd.target)
        elif cmd.kind in ("mute", "unmute"):
            self._set_muted(cmd.kind == "mute")

    def dispatch(self, text: str, decision: RouteDecision) -> tuple[str, str]:
        turn_id = self.registry.mint_turn_id()
        self.bus.emit(
            "route.decision",
            {"turn_id": turn_id, "kind": decision.kind, "text": text},
        )
        target = (
            self.registry.by_call_name(decision.target) if decision.target else None
        )
        result = self.registry.dispatch(text, turn_id, target=target)
        return turn_id, result

    # ---- shared mic/duplex state changes ---------------------------------------

    def _set_duplex(self, mode: str) -> None:
        duplex = DuplexMode(mode)  # raises on garbage — caller surfaces it
        if self.voice is not None:
            self.voice.set_duplex(duplex)
        self.bus.emit("mic.state", {"mode": mode})

    def _set_muted(self, muted: bool) -> None:
        if self.voice is not None:
            self.voice.suppress_mic(muted)
        self.bus.emit("mic.state", {"mode": "mute" if muted else "unmute"})

    # ---- control commands (CLI/WS) -----------------------------------------------

    def _control(self, cmd: str, payload: dict) -> dict:
        if cmd == "switch_session":
            s = self.registry.switch(str(payload.get("name", "")))
            if s is None:
                raise ValueError(f"no session named {payload.get('name')!r}")
            return {"active": s.session_id, "name": s.call_name}
        if cmd == "interrupt":
            if self.voice is not None:
                self.voice.barge_in()
            return {}
        if cmd == "mic.set":
            mode = str(payload.get("mode", ""))
            if mode not in (DuplexMode.FULL.value, DuplexMode.HALF.value):
                raise ValueError(f"unknown mic mode {mode!r}")
            self._set_duplex(mode)
            return {"mode": mode}
        if cmd == "say_as_user":
            text = str(payload.get("text", "")).strip()
            if not text:
                raise ValueError("text required")
            asyncio.get_running_loop().create_task(self._route_and_dispatch(text))
            return {}
        if cmd == "config.get":
            return self._public_config()
        if cmd == "config.set":
            raise ValueError("config.set lands with persistent config (M3)")
        raise ValueError(f"unknown command {cmd!r}")

    def _public_config(self) -> dict:
        """Config snapshot minus secrets (tokens, api keys)."""
        out: dict[str, Any] = {}
        for section, values in self.cfg.items():
            if not isinstance(values, dict):
                continue
            out[section] = {
                k: v for k, v in values.items() if "token" not in k and "key" not in k
            }
        return out

    # ---- typed input path (UI text box / voco input) --------------------------------

    async def _route_and_dispatch(self, text: str) -> None:
        routed = await self.route(text)
        if routed.phrase is not None:
            self.run_phrase(routed.phrase)
            return
        decision = routed.decision or RouteDecision(kind="forward")
        if decision.kind == "answer":
            if decision.speech and self.voice is not None:
                self.voice.speak_local(decision.speech, None)
            return
        turn_id, result = self.dispatch(text, decision)
        if self.voice is not None:
            self.voice.dispatch_feedback(turn_id, result)
            if decision.kind == "ack_forward" and decision.speech:
                self.voice.speak_local(decision.speech, turn_id)

    # ---- agent says become speech --------------------------------------------------

    def _wire_say_speech(self) -> None:
        def on_event(env) -> None:
            if env.type != "agent.say" or not env.payload.get("active"):
                return
            if self.voice is not None:
                self.voice.speak_agent(env.payload["text"], env.payload.get("turn_id"))

        self.bus.subscribe(on_event)

    # ---- run ---------------------------------------------------------------

    async def run(self, host: str = "127.0.0.1", port: int = 7777) -> None:
        loop = asyncio.get_running_loop()
        self._wire_say_speech()
        runner = await run_server(self.bridge, host=host, port=port)
        if not self.no_audio:
            from voco.voice_loop import VoiceLoop

            try:
                self.voice = VoiceLoop(self.cfg, self.bus, host=self)
                await self.voice.start(loop)
            except Exception as e:
                self.voice = None
                self.bus.emit("daemon.error", {"error": f"voice loop unavailable: {e}"})
                print(f"voco-d: voice loop unavailable ({e}); running headless")
        print(
            f"voco-d listening on {host}:{port}"
            + (" (no audio)" if self.voice is None else "")
        )
        try:
            await asyncio.Event().wait()
        finally:
            await self.bridge.shutdown()
            await runner.cleanup()
            if self.voice is not None:
                self.voice.stop()


def main() -> None:
    parser = argparse.ArgumentParser(prog="voco-d")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--port", type=int, default=7777)
    parser.add_argument("--no-audio", action="store_true")
    args = parser.parse_args()
    cfg = load_config(args.config)
    daemon = Daemon(cfg, no_audio=args.no_audio)
    try:
        asyncio.run(daemon.run(port=args.port))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
