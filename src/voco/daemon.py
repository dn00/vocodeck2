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
import signal
import sys
import time
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from voco.adapters.tmux import TmuxManager
    from voco.voice_loop import VoiceLoop

from voco import config as config_mod
from voco.adapters.state_store import StateStore
from voco.core.arbitration import DuplexMode
from voco.core.attention import AttentionMode
from voco.core.events import EventBus
from voco.core.first_mate import build_grounding, execute_action
from voco.core.phrases import PhraseCommand
from voco.core.registry import Registry
from voco.core.router import Routed, Router
from voco.core.turn import RouteDecision
from voco.server.http import BridgeServer, run_server

DEFAULT_CONFIG = Path.home() / ".config" / "voco" / "config.toml"


def load_config(path: Path | None) -> dict[str, Any]:
    """Read base + .local.toml overrides, validate, refuse boot on errors."""
    p = path or DEFAULT_CONFIG
    if not p.exists():
        if path is not None:  # explicit --config that doesn't exist is an error
            raise SystemExit(f"voco-d: config not found: {p}")
        base: dict[str, Any] = {}
    else:
        try:
            base = tomllib.loads(p.read_text())
        except tomllib.TOMLDecodeError as e:
            raise SystemExit(f"voco-d: bad config {p}: {e}") from e
    try:
        overrides = config_mod.read_overrides(config_mod.overrides_path(p))
    except tomllib.TOMLDecodeError as e:
        raise SystemExit(
            f"voco-d: bad overrides {config_mod.overrides_path(p)}: {e}"
        ) from e
    cfg = config_mod.merge(base, overrides)
    errors, warnings = config_mod.validate(cfg)
    for w in warnings:
        print(f"voco-d: config warning: {w}", file=sys.stderr)
    if errors:
        listing = "\n  ".join(errors)
        raise SystemExit(f"voco-d: invalid config {p}:\n  {listing}")
    return cfg


class Daemon:
    def __init__(
        self,
        cfg: dict[str, Any],
        no_audio: bool = False,
        config_path: Path | None = None,
    ) -> None:
        self.cfg = cfg
        self.no_audio = no_audio
        self._config_path = config_path or DEFAULT_CONFIG
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
                json_mode=bool(mate_cfg.get("json_mode", True)),
            )
        self.router = Router(
            first_mate=mate,
            timeout_s=float((mate_cfg or {}).get("timeout_ms", 800)) / 1000.0,
        )
        self.bridge = BridgeServer(
            self.registry,
            self.bus,
            token=cfg.get("bridge", {}).get("token"),
            on_control=self._control,
        )
        self.voice: VoiceLoop | None = None
        self._tmux_mgr: TmuxManager | None = None
        self._port = 7777
        self._state = StateStore(
            Path(
                cfg.get("state", {}).get(
                    "dir", Path.home() / ".local" / "state" / "voco"
                )
            )
        )
        self._state_save_task: asyncio.Task[None] | None = None
        self._watcher_task: asyncio.Task[None] | None = None

    def _tmux(self) -> TmuxManager:
        if self._tmux_mgr is None:
            from voco.adapters.tmux import TmuxManager

            self._tmux_mgr = TmuxManager(voco_url=f"http://127.0.0.1:{self._port}")
        return self._tmux_mgr

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
            self._inject_escape(self.registry.active)
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
        session = target or self.registry.active
        result = self.registry.dispatch(text, turn_id, target=target)
        if result == "queued_idle" and session is not None:
            self._schedule_nudge(session.session_id)
        return turn_id, result

    # ---- inject capability (tmux send-keys; SPEC v2 pulled forward) ---------

    NUDGE_TEXT = (
        "[voco] queued voice input waiting — call voice_listen "
        "(or run `voco listen`) to receive it, then keep listening."
    )

    def _inject_escape(self, session) -> None:
        """Voice 'stop' becomes a real interrupt for inject-capable sessions."""
        if session is None or session.inject_target is None:
            return
        host = session.identity.get("host_alias")  # None = local
        loop = asyncio.get_running_loop()
        loop.run_in_executor(
            None, self._inject_safely, session.inject_target, "escape", host
        )

    def _schedule_nudge(self, session_id: str) -> None:
        """Self-healing rearm: if input is still queued shortly after landing
        on an idle inject-capable session, type a nudge into its composer."""

        async def nudge() -> None:
            await asyncio.sleep(2.0)
            s = self.registry.get(session_id)
            if s is None or s.state != "idle" or not s.queued:
                return  # it picked the input up (or left) — stand down
            if s.inject_target is None:
                return
            host = s.identity.get("host_alias")
            await asyncio.get_running_loop().run_in_executor(
                None, self._inject_safely, s.inject_target, "nudge", host
            )

        asyncio.get_running_loop().create_task(nudge())

    def _inject_safely(self, target: str, kind: str, host: str | None) -> None:
        try:
            if kind == "escape":
                self._tmux().send_escape(target, host=host)
            else:
                self._tmux().send_text(target, self.NUDGE_TEXT, host=host)
        except Exception as e:
            # Named fail-silent: inject is best-effort; the queue + earcon
            # remain the source of truth (SPEC §8.4).
            self.bus.emit("daemon.error", {"error": f"inject: {e}"})

    # ---- shared mic/duplex state changes ---------------------------------------

    def _set_duplex(self, mode: str) -> None:
        duplex = DuplexMode(mode)  # raises on garbage — caller surfaces it
        if self.voice is not None:
            self.voice.set_duplex(duplex)
        self._emit_mic_state()

    def _set_muted(self, muted: bool) -> None:
        if self.voice is not None:
            self.voice.set_muted(muted)
        self._emit_mic_state()

    def _emit_mic_state(self) -> None:
        v = self.voice
        self.bus.emit(
            "mic.state",
            {
                "duplex": v.duplex.value if v else None,
                "attention": v.attention.mode.value if v else None,
            },
        )

    # ---- control commands (CLI/WS) -----------------------------------------------

    @staticmethod
    async def _run_blocking(fn):
        """Subprocess-backed commands (tmux/ssh) must never block the loop
        that pumps WS events, listen polls, and speech."""
        return await asyncio.get_running_loop().run_in_executor(None, fn)

    async def _control(self, cmd: str, payload: dict) -> dict:
        if cmd == "switch_session":
            s = self.registry.switch(str(payload.get("name", "")))
            if s is None:
                raise ValueError(f"no session named {payload.get('name')!r}")
            return {"active": s.session_id, "name": s.call_name}
        if cmd == "interrupt":
            if self.voice is not None:
                self.voice.barge_in()
            self._inject_escape(self.registry.active)
            return {}
        if cmd == "mic.set":
            # Two orthogonal knobs (SPEC §4.4/§4.5); legacy "mode" = duplex.
            duplex = payload.get("duplex") or payload.get("mode")
            attention = payload.get("attention")
            if duplex is not None:
                self._set_duplex(str(duplex))
            if attention is not None:
                if self.voice is None:
                    raise ValueError("no voice loop running")
                self.voice.set_attention(AttentionMode(str(attention)))
                self._emit_mic_state()
            if duplex is None and attention is None:
                raise ValueError("mic.set needs duplex and/or attention")
            return {"duplex": duplex, "attention": attention}
        if cmd == "say_as_user":
            text = str(payload.get("text", "")).strip()
            if not text:
                raise ValueError("text required")
            asyncio.get_running_loop().create_task(self._route_and_dispatch(text))
            return {}
        if cmd == "session.spawn":
            harness = str(payload.get("harness", "")).strip()
            if not harness:
                raise ValueError("harness command required")
            # Manager is constructed HERE (loop thread), used in the
            # executor — no shared-state mutation off the loop.
            mgr = self._tmux()
            name = await self._run_blocking(
                lambda: mgr.spawn(
                    harness,
                    name=str(payload.get("name") or harness),
                    cwd=payload.get("cwd"),
                    host=payload.get("host"),
                )
            )
            return {"tmux_session": name}
        if cmd == "session.kill":
            mgr = self._tmux()
            await self._run_blocking(
                lambda: mgr.kill(str(payload.get("name", "")), host=payload.get("host"))
            )
            return {}
        if cmd == "session.panes":
            mgr = self._tmux()
            panes = await self._run_blocking(lambda: mgr.list(host=payload.get("host")))
            return {"panes": panes}
        if cmd == "session.detach":
            name = str(payload.get("name", "")).strip()
            s = self.registry.by_call_name(name)
            if s is None:
                raise ValueError(f"no session named {name!r}")
            self.registry.detach(s.session_id)
            return {"detached": s.call_name}
        if cmd == "session.peek":
            from voco.core.pane_state import classify

            target, host = self._peek_target(payload)
            mgr = self._tmux()
            text = await self._run_blocking(lambda: mgr.capture_pane(target, host=host))
            return {"text": text, "hint": classify(text)}
        if cmd == "config.get":
            return self._public_config()
        if cmd == "config.set":
            return self._config_set(payload)
        raise ValueError(f"unknown command {cmd!r}")

    # Keys that take effect immediately; everything else persists and is
    # honestly reported restart_required (a wrong "applied" is worse).
    _HOT_APPLY: ClassVar[set[str]] = {
        "audio.duplex",
        "audio.attention",
        "first_mate.timeout_ms",
    }

    def _config_set(self, payload: dict) -> dict:
        key = str(payload.get("key", "")).strip()
        if "value" not in payload:
            raise ValueError("config.set needs key and value")
        value = payload["value"]
        self.cfg = config_mod.set_value(self._config_path, self.cfg, key, value)
        applied = False
        if key in self._HOT_APPLY:
            try:
                if key == "audio.duplex":
                    self._set_duplex(str(value))
                elif key == "audio.attention":
                    if self.voice is None:
                        raise ValueError("no voice loop running")
                    self.voice.set_attention(AttentionMode(str(value)))
                    self._emit_mic_state()
                elif key == "first_mate.timeout_ms":
                    self.router.set_timeout(float(value) / 1000.0)
                applied = True
            except Exception:
                applied = False  # persisted; takes effect on restart
        return {
            "key": key,
            "value": value,
            "applied": applied,
            "restart_required": not applied,
            "written": str(config_mod.overrides_path(self._config_path)),
        }

    def _peek_target(self, payload: dict) -> tuple[str, str | None]:
        """Terminal mirror target: a registered session's pane (by call
        name) or a raw tmux target (spawned but not yet attached)."""
        target = payload.get("target")
        host = payload.get("host")
        if not target:
            name = str(payload.get("name", "")).strip()
            s = self.registry.by_call_name(name)
            if s is None:
                raise ValueError(f"no session named {name!r}")
            if s.inject_target is None:
                raise ValueError(f"{s.call_name} has no terminal to peek (not in tmux)")
            target = s.inject_target
            host = s.identity.get("host_alias")
        return str(target), host

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

    # ---- durable sessions (SPEC §13 queue-loss gap closed) ------------------------

    _STATE_EVENTS: ClassVar[set[str]] = {
        "session.attached",
        "session.detached",
        "session.activated",
        "session.state",
        "input.queued",
        "agent.say",
        "screen.updated",
        "digest.updated",
    }

    def _restore_state(self) -> None:
        data, err = self._state.load()
        if err:
            self.bus.emit("daemon.error", {"error": f"state: {err}"})
        if data:
            n = self.registry.restore(data)
            if n:
                print(f"voco-d: restored {n} session(s) from {self._state.path}")

    def _wire_state_saver(self) -> None:
        def on_event(env) -> None:
            if env.type in self._STATE_EVENTS:
                self._schedule_state_save()

        self.bus.subscribe(on_event)

    def _schedule_state_save(self) -> None:
        """Debounced: one pending save absorbs every change in its window
        (dump happens after the sleep, on the loop — always consistent)."""
        if self._state_save_task is not None and not self._state_save_task.done():
            return

        async def save_soon() -> None:
            await asyncio.sleep(0.5)
            data = self.registry.dump()
            try:
                await self._run_blocking(lambda: self._state.save(data))
            except Exception as e:
                self.bus.emit("daemon.error", {"error": f"state save: {e}"})

        self._state_save_task = asyncio.get_running_loop().create_task(save_soon())

    # ---- pane watcher (proactive eyes on unattended terminals) --------------------

    def _start_watcher(self, loop: asyncio.AbstractEventLoop) -> None:
        cfg = self.cfg.get("watcher", {})
        if not cfg.get("enabled", True):
            return
        from voco.watcher import PaneWatcher

        watcher = PaneWatcher(
            self.registry,
            self._tmux(),
            interval_s=float(cfg.get("interval_s", 3.0)),
            on_waiting=self._on_pane_waiting if cfg.get("speak", True) else None,
        )
        self._watcher_task = loop.create_task(watcher.run())

    def _on_pane_waiting(self, session) -> None:
        """Confirmed waiting edge: say so. Loop-domain speech — an observed
        fact about the deck, hedged because it comes from heuristics."""
        if self.voice is not None:
            self.voice.speak_local(
                f"{session.call_name} looks like they're waiting on you.", None
            )

    # ---- operational errors reach the operator, not just the bus -----------------

    def _wire_error_log(self) -> None:
        def on_event(env) -> None:
            if env.type == "daemon.error":
                stamp = time.strftime("%H:%M:%S", time.localtime(env.ts))
                print(
                    f"voco-d {stamp} ERROR {env.payload.get('error', '?')}",
                    file=sys.stderr,
                )

        self.bus.subscribe(on_event)

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
        self._port = port
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:
                pass  # Windows: KeyboardInterrupt in main() is the fallback
        self._wire_say_speech()
        self._wire_error_log()
        self._restore_state()
        self._wire_state_saver()
        self._start_watcher(loop)
        try:
            runner = await run_server(self.bridge, host=host, port=port)
        except OSError as e:
            raise SystemExit(
                f"voco-d: cannot bind {host}:{port} ({e.strerror or e}); "
                "is another voco-d running?"
            ) from e
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
            await stop.wait()
        finally:
            # Order matters: unpark agents first (they exit their listen
            # cleanly), then close the server, then the audio shell.
            await self.bridge.shutdown()
            await runner.cleanup()
            if self.voice is not None:
                self.voice.stop()
            if self._watcher_task is not None:
                self._watcher_task.cancel()
            if self._state_save_task is not None:
                self._state_save_task.cancel()
            try:
                self._state.save(self.registry.dump())  # final synchronous save
            except Exception as e:
                print(f"voco-d: state save failed: {e}", file=sys.stderr)
            print("voco-d: shut down cleanly")


def main() -> None:
    parser = argparse.ArgumentParser(prog="voco-d")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--port", type=int, default=7777)
    parser.add_argument("--no-audio", action="store_true")
    args = parser.parse_args()
    cfg = load_config(args.config)
    daemon = Daemon(cfg, no_audio=args.no_audio, config_path=args.config)
    try:
        asyncio.run(daemon.run(port=args.port))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
