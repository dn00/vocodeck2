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
import os
import signal
import sys
import time
import tomllib
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from voco.adapters.tmux import TmuxManager
    from voco.adapters.worktree import WorktreeManager
    from voco.voice_loop import VoiceLoop

from voco import config as config_mod
from voco.adapters.state_store import StateStore
from voco.core.arbitration import DuplexMode
from voco.core.attention import AttentionMode
from voco.core.events import EventBus
from voco.core.first_mate import build_grounding, execute_action
from voco.core.phrases import PhraseCommand
from voco.core.registry import Registry, Session
from voco.core.router import Routed, Router
from voco.core.turn import RouteDecision
from voco.core.workspace import Workspace, WorkspaceStore
from voco.server.http import BridgeServer, run_server
from voco.server.workbench import handle_workbench_command

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
        self.workspaces = WorkspaceStore(emit=self.bus.emit)
        mate = None
        mate_cfg = cfg.get("first_mate")
        if mate_cfg:
            from voco.adapters.first_mate import OpenAIChatFirstMate

            # Socket budget covers the LATE window, not just the router
            # timeout: a mate that misses the dispatch deadline keeps
            # running in the background and still acts/speaks/corrects.
            budget_ms = max(
                float(mate_cfg.get("timeout_ms", 800)),
                float(mate_cfg.get("late_window_ms", 8000)),
            )
            mate = OpenAIChatFirstMate(
                base_url=mate_cfg["base_url"],
                model=mate_cfg.get("model", ""),
                api_key=mate_cfg.get("api_key"),
                json_mode=bool(mate_cfg.get("json_mode", True)),
                total_timeout_s=budget_ms / 1000.0 + 1.5,
            )
        self.router = Router(
            first_mate=mate,
            timeout_s=float((mate_cfg or {}).get("timeout_ms", 800)) / 1000.0,
        )
        # Latest routing awaiting its dispatch turn_id (late-mate stamping).
        self._pending_late: dict[str, Any] | None = None
        self.bridge = BridgeServer(
            self.registry,
            self.bus,
            token=cfg.get("bridge", {}).get("token"),
            on_control=self._control,
            snapshot_extra=lambda: {"mic": self._mic_payload()},
            workspaces=self.workspaces,
            allowed_origins=cfg.get("server", {}).get("allowed_origins"),
        )
        self.voice: VoiceLoop | None = None
        self._tmux_mgr: TmuxManager | None = None
        # tmux session -> worktree path, for the sessions THIS run spawned
        # with --worktree (in-memory by design: after a restart we no longer
        # know we created it, and not-knowing fails safe — no removal).
        self._worktree_mgr: WorktreeManager | None = None
        self._spawned_worktrees: dict[str, str] = {}
        # Per-workspace primary-review override (§4.3 UI selector):
        # workspace key -> call_name. In-memory routing preference.
        self._primary_override: dict[str, str] = {}
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
        # Workbench manifests (SPEC-WORKBENCH §8): durable pages + findings.
        from voco.adapters.manifest import WorkspaceManifest

        self._manifest = WorkspaceManifest(self._workspace_data_dir())
        self._manifest_locked = False
        self._manifest_save_task: asyncio.Task[None] | None = None
        self._dirty_workspaces: set[str] = set()

    def _tmux(self) -> TmuxManager:
        if self._tmux_mgr is None:
            from voco.adapters.tmux import TmuxManager

            self._tmux_mgr = TmuxManager(voco_url=f"http://127.0.0.1:{self._port}")
        return self._tmux_mgr

    def _worktrees_mgr(self) -> WorktreeManager:
        if self._worktree_mgr is None:
            from voco.adapters.worktree import WorktreeManager

            self._worktree_mgr = WorktreeManager()
        return self._worktree_mgr

    async def _reap_worktree(self, tmux_name: str) -> dict[str, Any]:
        """After a kill: remove the worktree THIS daemon run created for
        that session — clean ones only (W3; dirty work is sacred). Not
        knowing the worktree (restart wiped the map) means keeping it."""
        wt_path = self._spawned_worktrees.pop(tmux_name, None)
        if wt_path is None:
            return {}
        from voco.adapters.worktree import WorktreeError

        try:
            await self._run_blocking(lambda: self._worktrees_mgr().remove(wt_path))
        except WorktreeError as e:
            self._spawned_worktrees[tmux_name] = wt_path  # still ours
            return {"worktree": wt_path, "worktree_kept": str(e)}
        return {"worktree": wt_path, "worktree_removed": True}

    # ---- VoiceHost port (decisions stay here; audio reacts in VoiceLoop) ----

    async def route(self, text: str) -> Routed:
        """Decide + execute any first-mate action (loop-domain, immediate).

        The mate is bounded by timeout_ms for the DISPATCH decision only:
        past that, the fast path (phrase table + name heuristics) routes
        immediately and the mate finishes in the background
        (_on_late_mate) — it must never slow the action (triage
        2026-07-03)."""
        grounding = build_grounding(
            self.registry,
            self.voice.duplex.value if self.voice else "headless",
            time.time(),
        )
        channel = None
        sink = None
        if self.voice is not None and bool(
            self.cfg.get("first_mate", {}).get("stream", False)
        ):
            channel = self.voice.open_mate_speech_channel()
            sink = channel.push
        ctx: dict[str, Any] = {
            "text": text,
            "channel": channel,
            "turn_id": None,
            "dispatched_to": None,
        }
        routed = await self.router.decide(
            text,
            self.registry.call_names(),
            grounding,
            speech_sink=sink,
            on_late=lambda d: self._on_late_mate(d, ctx),
        )
        if routed.late_pending:
            # Mate still running: leave its speech channel open (it may be
            # mid-sentence); dispatch() stamps the turn; the late handler
            # finishes or cancels.
            self._pending_late = ctx
            return routed
        if channel is not None:
            d = routed.decision
            # Only mate kinds that SPEAK get their stream kept; a timeout
            # coercion or plain forward drops un-spoken text. Streamed
            # speech blanks the decision so downstream never double-speaks.
            if (
                d is not None
                and d.kind in ("answer", "ack_forward")
                and (channel.finish())
            ):
                routed = Routed(phrase=routed.phrase, decision=replace(d, speech=""))
            else:
                channel.cancel()
        if routed.decision is not None and routed.decision.action is not None:
            execute_action(
                routed.decision.action,
                self.registry,
                set_mic=self._set_duplex,
                set_muted=self._set_muted,
            )
        return routed

    def _on_late_mate(self, decision: RouteDecision | None, ctx: dict) -> None:
        """Mate finished after dispatch went with the fast path. Late is
        still useful: actions execute (idempotent deck ops), answers and
        acks speak (TTL + rule-3 police staleness), and a targeted
        decision that disagrees with where the words landed re-dispatches
        with a spoken correction."""
        if self._pending_late is ctx:
            self._pending_late = None
        channel = ctx.get("channel")
        if decision is None:  # mate failed/garbled: nothing to add
            if channel is not None:
                channel.cancel()
            return
        if decision.action is not None:
            try:
                execute_action(
                    decision.action,
                    self.registry,
                    set_mic=self._set_duplex,
                    set_muted=self._set_muted,
                )
            except Exception as e:
                self.bus.emit("daemon.error", {"error": f"late mate action: {e}"})
        if self._late_reroute(decision, ctx):
            return
        turn_id = ctx.get("turn_id")
        streamed = False
        if channel is not None:
            if decision.kind in ("answer", "ack_forward"):
                streamed = channel.finish()
            else:
                channel.cancel()
        if (
            not streamed
            and decision.kind in ("answer", "ack_forward")
            and decision.speech.strip()
            and self.voice is not None
        ):
            self.voice.speak_local(decision.speech, turn_id)

    def _late_reroute(self, decision: RouteDecision, ctx: dict) -> bool:
        """Late mate says the words belonged to someone else: send them
        there too, say so, and drop the (wrong-context) streamed ack."""
        target = decision.target
        if decision.kind not in ("forward", "ack_forward") or not target:
            return False
        session = self.registry.by_call_name(target)
        if session is None:
            return False
        landed = ctx.get("dispatched_to") or ""
        if target.lower() == landed.lower():
            return False
        turn_id = self.registry.mint_turn_id()
        self.bus.emit(
            "route.decision",
            {
                "turn_id": turn_id,
                "kind": "forward",
                "text": ctx["text"],
                "origin": "voice",
                "late_reroute": True,
            },
        )
        result = self.registry.dispatch(ctx["text"], turn_id, target=session)
        channel = ctx.get("channel")
        if channel is not None:
            channel.cancel()
        if self.voice is not None:
            self.voice.dispatch_feedback(turn_id, result)
            self.voice.speak_local(
                f"That was for {session.call_name} — rerouted.", turn_id
            )
        return True

    def run_phrase(self, cmd: PhraseCommand) -> None:
        if cmd.kind == "stop":
            if self.voice is not None:
                self.voice.barge_in()
            self._inject_escape(self.registry.active)
        elif cmd.kind == "switch" and cmd.target:
            self.registry.switch(cmd.target)
        elif cmd.kind in ("mute", "unmute"):
            self._set_muted(cmd.kind == "mute")

    def dispatch(
        self, text: str, decision: RouteDecision, origin: str = "voice"
    ) -> tuple[str, str]:
        turn_id = self.registry.mint_turn_id()
        self.bus.emit(
            "route.decision",
            {"turn_id": turn_id, "kind": decision.kind, "text": text, "origin": origin},
        )
        target = (
            self.registry.by_call_name(decision.target) if decision.target else None
        )
        session = target or self.registry.active
        result = self.registry.dispatch(text, turn_id, target=target, origin=origin)
        # A pending late-mate routing gets its turn identity the moment the
        # fast path dispatches — rules 2/3 + reroute checks need it.
        ctx = self._pending_late
        if ctx is not None and ctx.get("text") == text and ctx.get("turn_id") is None:
            ctx["turn_id"] = turn_id
            ctx["dispatched_to"] = session.call_name if session else None
            channel = ctx.get("channel")
            if channel is not None:
                channel.set_turn_id(turn_id)
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

    def _mic_payload(self) -> dict:
        v = self.voice
        return {
            "duplex": v.duplex.value if v else None,
            "attention": v.attention.mode.value if v else None,
        }

    def _emit_mic_state(self) -> None:
        self.bus.emit("mic.state", self._mic_payload())

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
            if duplex is None and attention is None:
                raise ValueError("mic.set needs duplex and/or attention")
            if self.voice is None:
                # There is no runtime to change; pretending otherwise is the
                # exact lie the live test caught on config.set.
                raise ValueError("no voice loop running")
            if duplex is not None:
                self._set_duplex(str(duplex))
            if attention is not None:
                self.voice.set_attention(AttentionMode(str(attention)))
                self._emit_mic_state()
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
            cwd = payload.get("cwd")
            wt = payload.get("worktree")
            # A worktree spawn names the session after its branch.
            display = str(payload.get("name") or (wt or {}).get("branch") or harness)
            wt_path: str | None = None
            if wt:
                # W3: create the worktree first, spawn inside it. Local
                # only — a remote host's disk is not ours to branch on.
                if payload.get("host"):
                    raise ValueError("--worktree is local-only (no --host)")
                if not cwd:
                    raise ValueError("worktree spawn needs the source repo cwd")
                from voco.adapters.worktree import WorktreeError

                wmgr = self._worktrees_mgr()
                branch = str(wt.get("branch", "")).strip()
                base = wt.get("from")
                try:
                    wt_path = await self._run_blocking(
                        lambda: wmgr.add(str(cwd), branch, base)
                    )
                except WorktreeError as e:
                    raise ValueError(str(e)) from e  # caller error → 400
                cwd = wt_path
            try:
                name = await self._run_blocking(
                    lambda: mgr.spawn(
                        harness,
                        name=display,
                        cwd=cwd,
                        host=payload.get("host"),
                    )
                )
            except Exception:
                if wt_path is not None:
                    # The worktree was created for THIS spawn; a dead spawn
                    # must not strand it (it is minutes old and clean).
                    try:
                        await self._run_blocking(
                            lambda: self._worktrees_mgr().remove(wt_path)
                        )
                    except Exception as e:
                        self.bus.emit(
                            "daemon.error", {"error": f"worktree cleanup: {e}"}
                        )
                raise
            if wt_path is not None:
                self._spawned_worktrees[name] = wt_path
            out = {"tmux_session": name}
            if wt_path is not None:
                out["worktree"] = wt_path
            return out
        if cmd == "session.kill":
            mgr = self._tmux()
            tmux_name = str(payload.get("name", ""))
            await self._run_blocking(
                lambda: mgr.kill(tmux_name, host=payload.get("host"))
            )
            return await self._reap_worktree(tmux_name)
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
        if cmd == "review.primary":
            return self._set_primary_override(payload)
        try:
            return handle_workbench_command(
                self.workspaces, cmd, payload, data_dir=self._workspace_data_dir()
            )
        except KeyError:
            pass  # not a workbench command; fall through
        raise ValueError(f"unknown command {cmd!r}")

    def _workspace_data_dir(self) -> Path:
        base = self.cfg.get("workbench", {}).get("data_dir")
        if base:
            return Path(base)
        env = os.environ.get("VOCO_DATA_DIR")
        return Path(env) if env else Path.home() / ".local" / "share" / "voco"

    # Keys that take effect immediately; everything else persists and is
    # honestly reported restart_required (a wrong "applied" is worse).
    _HOT_APPLY: ClassVar[set[str]] = {
        "audio.duplex",
        "audio.attention",
        "audio.dispatch_hold_ms",
        "audio.incomplete_hold_ms",
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
                    if self.voice is None:
                        raise ValueError("no voice loop running")
                    self._set_duplex(str(value))
                elif key == "audio.attention":
                    if self.voice is None:
                        raise ValueError("no voice loop running")
                    self.voice.set_attention(AttentionMode(str(value)))
                    self._emit_mic_state()
                elif key == "audio.dispatch_hold_ms":
                    if self.voice is None:
                        raise ValueError("no voice loop running")
                    self.voice.set_patience(hold_ms=int(value))
                elif key == "audio.incomplete_hold_ms":
                    if self.voice is None:
                        raise ValueError("no voice loop running")
                    self.voice.set_patience(incomplete_ms=int(value))
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
        turn_id, result = self.dispatch(text, decision, origin="typed")
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

    # ---- workbench manifests (SPEC-WORKBENCH §8) ----------------------------

    _MANIFEST_EVENTS = frozenset(
        {
            "workspace.updated",
            "page.updated",
            "finding.added",
            "finding.updated",
            "ask.created",
            "ask.answered",
        }
    )

    def _restore_manifests(self) -> None:
        try:
            self._manifest.acquire()
            self._manifest_locked = True
        except Exception as e:
            # A live sibling daemon owns the data dir: run without durable
            # workbench state rather than clobber it (fail-soft, named).
            self.bus.emit("daemon.error", {"error": f"workspace lock: {e}"})
            print(f"voco-d: workbench persistence off ({e})", file=sys.stderr)
            return
        manifests, errors = self._manifest.load_all()
        for err in errors:
            self.bus.emit("daemon.error", {"error": f"manifest: {err}"})
        n = sum(1 for m in manifests if self.workspaces.restore_workspace(m))
        if n:
            print(f"voco-d: restored {n} workspace(s)")

    def _wire_manifest_saver(self) -> None:
        def on_event(env) -> None:
            if env.type in self._MANIFEST_EVENTS:
                key = env.payload.get("workspace") or env.payload.get("key")
                if key:
                    self._dirty_workspaces.add(key)
                    self._schedule_manifest_save()

        self.bus.subscribe(on_event)

    def _schedule_manifest_save(self) -> None:
        if not self._manifest_locked:
            return
        if self._manifest_save_task is not None and not self._manifest_save_task.done():
            return

        async def save_soon() -> None:
            await asyncio.sleep(0.5)
            self._flush_manifests()

        self._manifest_save_task = asyncio.get_running_loop().create_task(save_soon())

    def _flush_manifests(self) -> None:
        if not self._manifest_locked:
            return
        keys, self._dirty_workspaces = self._dirty_workspaces, set()
        for key in keys:
            ws = self.workspaces.get(key)
            if ws is None:
                continue
            try:
                self._manifest.save(key, self.workspaces.dump_workspace(ws))
            except Exception as e:
                self.bus.emit("daemon.error", {"error": f"manifest save: {e}"})

    # ---- the unified wake (SPEC-WORKBENCH §4.2/§4.3) ------------------------

    def _primary_session(self, ws: Workspace) -> Session | None:
        """Elect the workspace's primary review agent (§4.3): the UI
        override when set, else the active session if it lives here, else
        the sole review-capable session, else the most recently PARKED
        review-capable one (a parked listener can be woken now; a merely
        recently-seen one cannot). Election is a read — home_of never
        creates workspaces or emits."""
        here = [
            s
            for s in self.registry.all()
            if "review" in s.capabilities and self.workspaces.home_of(s.identity) is ws
        ]
        if not here:
            return None
        override = self._primary_override.get(ws.key)
        if override is not None:
            for s in here:
                if s.call_name == override:
                    return s
            # The overridden agent left: the override is stale, drop it.
            del self._primary_override[ws.key]
        active = self.registry.active
        if active is not None and active in here:
            return active
        if len(here) == 1:
            return here[0]
        pool = [s for s in here if s.parked] or here
        return max(pool, key=lambda s: s.last_seen)

    def _review_items_for(self, session_id: str) -> list[dict]:
        """Pending review items for a session: items on its OWN agent-scoped
        pages always; workspace-scoped items only when it is the primary
        (others read the shared ledger; no dup work). §4.3."""
        s = self.registry.get(session_id)
        if s is None or "review" not in s.capabilities:
            return []
        ws = self.workspaces.home_of(s.identity)
        if ws is None:
            return []
        primary = self._primary_session(ws) is s
        return [
            item
            for item in ws.pending_review()
            if item.get("agent") == s.call_name or ("agent" not in item and primary)
        ]

    def _wake_target(self, ws: Workspace, payload: dict) -> Session | None:
        """Who a new finding/ask wakes: an agent-scoped page's finding wakes
        THAT page's agent (§4.3); everything else wakes the primary."""
        page = ws.pages.get(str(payload.get("page_id") or ""))
        if page is not None and page.scope == "agent" and page.call_name:
            s = self.registry.by_call_name(page.call_name)
            if s is not None and "review" in s.capabilities:
                return s
            return None  # its agent is gone/ineligible; ledger still has it
        return self._primary_session(ws)

    def _wire_review_wake(self) -> None:
        self.registry.review_items = self._review_items_for

        def on_event(env) -> None:
            if env.type not in ("finding.added", "ask.created"):
                return
            key = env.payload.get("workspace")
            ws = self.workspaces.get(key) if key else None
            if ws is None:
                return
            target = self._wake_target(ws, env.payload)
            if target is not None:
                self.registry.wake_review(target.session_id)

        self.bus.subscribe(on_event)

    def _set_primary_override(self, payload: dict) -> dict:
        """review.primary {workspace, agent?}: pin (or clear) the primary
        review agent for a workspace. In-memory — a routing preference,
        not durable state."""
        key = str(payload.get("workspace", ""))
        ws = self.workspaces.get(key)
        if ws is None:
            raise ValueError(f"unknown workspace: {key}")
        agent = payload.get("agent")
        if not agent:
            self._primary_override.pop(key, None)
            return {"workspace": key, "primary": None}
        s = self.registry.by_call_name(str(agent))
        if s is None:
            raise ValueError(f"no session named {agent!r}")
        if "review" not in s.capabilities:
            raise ValueError(f"{s.call_name} has no review capability")
        if self.workspaces.home_of(s.identity) is not ws:
            raise ValueError(f"{s.call_name} is not in this workspace")
        self._primary_override[key] = s.call_name
        return {"workspace": key, "primary": s.call_name}

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
        self._restore_manifests()
        self._wire_manifest_saver()
        self._wire_review_wake()
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
            if self._manifest_save_task is not None:
                self._manifest_save_task.cancel()
            try:
                self._state.save(self.registry.dump())  # final synchronous save
            except Exception as e:
                print(f"voco-d: state save failed: {e}", file=sys.stderr)
            if self._manifest_locked:
                # Flush every workspace touched this run, then drop the lock.
                self._dirty_workspaces.update(self.workspaces.dirty_keys())
                self._flush_manifests()
                self._manifest.release()
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
