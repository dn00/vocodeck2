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
import importlib.metadata
import logging
import os
import signal
import sys
import time
import tomllib
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from voco.adapters.ptyterm import PtyBackend, PtyProcess
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

log = logging.getLogger("voco.daemon")

try:
    VERSION = importlib.metadata.version("voco")
except importlib.metadata.PackageNotFoundError:  # not installed (source tree)
    VERSION = "0+unknown"


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
        log.warning("config warning: %s", w)
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
        self._route_epoch = 0
        self._started_mono = time.monotonic()
        self.bridge = BridgeServer(
            self.registry,
            self.bus,
            token=cfg.get("bridge", {}).get("token"),
            on_control=self._control,
            snapshot_extra=lambda: {"mic": self._mic_payload()},
            workspaces=self.workspaces,
            allowed_origins=cfg.get("server", {}).get("allowed_origins"),
            health_info=self._health_info,
        )
        self.bridge.pty_lookup = self._pty_lookup  # /v1/term (W4)
        # B1b: url-mode artifacts iframe arbitrary origins — off by default.
        self.bridge.allow_artifact_urls = bool(
            cfg.get("workbench", {}).get("allow_artifact_urls", False)
        )
        self.voice: VoiceLoop | None = None
        # P3: the managed TTS floor (FloorSupervisor | None)
        self._floor: Any = None
        self._tmux_mgr: TmuxManager | None = None
        # tmux session -> worktree path, for worktrees this daemon lineage
        # created. This rides the protected state file so clean worktrees can
        # still be reaped after a daemon restart.
        self._worktree_mgr: WorktreeManager | None = None
        self._spawned_worktrees: dict[str, str] = {}
        # PTY terminals this daemon run owns (W4). Lazy like tmux.
        self._pty: PtyBackend | None = None
        # Per-workspace primary-review override (§4.3 UI selector):
        # workspace key -> call_name. In-memory routing preference.
        self._primary_override: dict[str, str] = {}
        # Live-git tracking (W5): per-workspace opt-out; default tracks.
        self._live_workspaces: dict[str, bool] = {}
        self._live_git_task: asyncio.Task[None] | None = None
        self._port = 7777
        self._state = StateStore(
            Path(
                cfg.get("state", {}).get(
                    "dir", Path.home() / ".local" / "state" / "voco"
                )
            )
        )
        self._state_locked = False
        self._state_save_task: asyncio.Task[None] | None = None
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._watcher_task: asyncio.Task[None] | None = None
        self._session_liveness_task: asyncio.Task[None] | None = None
        # Workbench manifests (SPEC-WORKBENCH §8): durable pages + findings.
        from voco.adapters.manifest import WorkspaceManifest

        self._manifest = WorkspaceManifest(self._workspace_data_dir())
        self._manifest_locked = False
        self._manifest_save_task: asyncio.Task[None] | None = None
        self._dirty_workspaces: set[str] = set()
        # U2a: gh link detection — optional edge, injectable for tests.
        from voco.adapters.ghlink import detect as ghlink_detect

        self._ghlink_detect = ghlink_detect
        self._gh_checked: set[str] = set()

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

    def _pty_backend(self) -> PtyBackend:
        if self._pty is None:
            from voco.adapters.ptyterm import PtyBackend

            self._pty = PtyBackend()
        return self._pty

    def _pty_for(self, session: Session) -> PtyProcess | None:
        """The daemon-owned pty behind a session, if we spawned one: the
        spawn baked its handle into the identity `instance`."""
        inst = str(session.identity.get("instance") or "")
        if inst.startswith("pty-") and self._pty is not None:
            return self._pty.get(inst)
        return None

    def _pty_lookup(self, session_id: str) -> PtyProcess | None:
        """The bridge's /v1/term route resolves sessions through this."""
        s = self.registry.get(session_id)
        return self._pty_for(s) if s is not None else None

    def _term_cells(self, session: Session) -> dict | None:
        """Terminal capability cells for the snapshot (SPEC-WORKBENCH §5)."""
        from voco.adapters.terminal import PTY_CELLS, TMUX_CELLS

        if self._pty_for(session) is not None:
            return PTY_CELLS.to_dict()
        if session.inject_target is not None:
            return TMUX_CELLS.to_dict()
        return None

    async def _reap_worktree(self, tmux_name: str) -> dict[str, Any]:
        """After a kill, remove a worktree this daemon lineage created for
        that session — clean ones only (W3; dirty work is sacred). Ownership
        survives restart in the protected state file; unknown paths stay."""
        wt_path = self._spawned_worktrees.pop(tmux_name, None)
        if wt_path is None:
            return {}
        from voco.adapters.worktree import WorktreeError

        try:
            await self._run_blocking(lambda: self._worktrees_mgr().remove(wt_path))
        except WorktreeError as e:
            self._spawned_worktrees[tmux_name] = wt_path  # still ours
            return {"worktree": wt_path, "worktree_kept": str(e)}
        self._schedule_state_save()
        return {"worktree": wt_path, "worktree_removed": True}

    # ---- VoiceHost port (decisions stay here; audio reacts in VoiceLoop) ----

    async def route(self, text: str) -> Routed:
        """Decide + execute any first-mate action (loop-domain, immediate).

        The mate is bounded by timeout_ms for the DISPATCH decision only:
        past that, the fast path (phrase table + name heuristics) routes
        immediately and the mate finishes in the background
        (_on_late_mate) — it must never slow the action (triage
        2026-07-03)."""
        # Every new turn invalidates the previous late callback. A model
        # answer from an older turn may finish, but it can no longer speak or
        # mutate current state.
        self._route_epoch += 1
        previous = self._pending_late
        self._pending_late = None
        if previous is not None and previous.get("channel") is not None:
            previous["channel"].cancel()

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
            "epoch": self._route_epoch,
        }
        routed = await self.router.decide(
            text,
            self.registry.call_names(),
            grounding,
            speech_sink=sink,
            on_late=lambda d: self._on_late_mate(d, ctx),
        )
        if routed.late_pending:
            if ctx["epoch"] != self._route_epoch:
                if channel is not None:
                    channel.cancel()
                return routed
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
        acknowledgement-only: it may speak for the same dispatched turn,
        but it can never execute an action or dispatch the command again."""
        channel = ctx.get("channel")
        if self._pending_late is not ctx or ctx.get("epoch") != self._route_epoch:
            if channel is not None:
                channel.cancel()
            return
        self._pending_late = None
        if decision is None:  # mate failed/garbled: nothing to add
            if channel is not None:
                channel.cancel()
            return
        turn_id = ctx.get("turn_id")
        if turn_id is None:
            if channel is not None:
                channel.cancel()
            return
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
        # fast path dispatches so an acknowledgement can be attributed.
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
        if session is None:
            return
        pp = self._pty_for(session)
        if pp is not None:
            try:
                pp.write(b"\x1b")
            except Exception as e:
                self.bus.emit("daemon.error", {"error": f"inject: {e}"})
            return
        if session.inject_target is None:
            return
        host = session.identity.get("host_alias")  # None = local

        async def inject() -> None:
            await asyncio.get_running_loop().run_in_executor(
                None, self._inject_safely, session.inject_target, "escape", host
            )

        self._spawn_background(inject(), name="inject-escape")

    def _schedule_nudge(self, session_id: str) -> None:
        """Self-healing rearm: if input is still queued shortly after landing
        on an idle inject-capable session, type a nudge into its composer."""

        async def nudge() -> None:
            await asyncio.sleep(2.0)
            s = self.registry.get(session_id)
            if s is None or s.state != "idle" or not s.queued:
                return  # it picked the input up (or left) — stand down
            pp = self._pty_for(s)
            if pp is not None:
                try:
                    pp.write(self.NUDGE_TEXT.encode() + b"\r")
                except Exception as e:
                    self.bus.emit("daemon.error", {"error": f"inject: {e}"})
                return
            if s.inject_target is None:
                return
            host = s.identity.get("host_alias")
            await asyncio.get_running_loop().run_in_executor(
                None, self._inject_safely, s.inject_target, "nudge", host
            )

        self._spawn_background(nudge(), name=f"nudge-{session_id[:8]}")

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
            # the deck filters "wake" out of its cycle when the detector
            # can't arm — an older server without this field keeps the
            # full cycle (strict === false check client-side)
            "wake_available": v.wake_available if v else False,
        }

    def _emit_mic_state(self) -> None:
        self.bus.emit("mic.state", self._mic_payload())

    # ---- control commands (CLI/WS) -----------------------------------------------

    def _spawn_background(self, coro: Any, *, name: str) -> asyncio.Task[Any]:
        """Own every fire-and-forget operation through daemon shutdown."""
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_done)
        return task

    def _background_done(self, task: asyncio.Task[Any]) -> None:
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            message = f"background task {task.get_name()} failed: {error}"
            log.error(message, exc_info=error)
            self.bus.emit("daemon.error", {"error": message})

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
        if cmd in ("ptt.press", "ptt.release"):
            # Client hold-PTT (mk3.1 #7): the deck's hold button / key use
            # the SAME machine path as the native hotkey, so attention
            # gating and turn semantics stay identical. Headless is an
            # honest error — there is no mic to gate.
            if self.voice is None:
                raise ValueError("no voice loop running")
            if cmd == "ptt.press":
                self.voice.ptt_press()
            else:
                self.voice.ptt_release()
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
            refused: str | None = None
            if duplex is not None:
                self._set_duplex(str(duplex))
            if attention is not None:
                if not self.voice.set_attention(AttentionMode(str(attention))):
                    refused = (
                        f"attention {attention!r} unavailable — "
                        f"{self.voice.wake_unavailable_reason}"
                    )
                self._emit_mic_state()
            result = self._mic_payload()  # actual state, never the echo
            if refused:
                result["refused"] = refused
            return result
        if cmd == "say_as_user":
            text = str(payload.get("text", "")).strip()
            if not text:
                raise ValueError("text required")
            await self._route_and_dispatch(text)
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
            backend = str(
                payload.get("backend")
                or self.cfg.get("terminal", {}).get("default_backend")
                or "tmux"
            )
            if backend not in ("tmux", "pty"):
                raise ValueError(f"backend must be tmux|pty, not {backend!r}")
            try:
                if backend == "pty":
                    # W4: daemon-owned pty — live streamed terminal page;
                    # local-only, dies with the daemon (stated in §5).
                    if payload.get("host"):
                        raise ValueError("pty backend is local-only; use tmux")
                    if sys.platform == "win32":
                        # ptyterm is Unix-only (fcntl/termios); ConPTY is
                        # the planned Windows path — a clean error beats
                        # an ImportError 500 meanwhile.
                        raise ValueError(
                            "pty backend needs Unix (Windows ConPTY pending)"
                            " — use tmux via WSL2"
                        )
                    from voco.adapters.ptyterm import PtyError

                    try:
                        pp = self._pty_backend().spawn(
                            harness,
                            cwd=str(cwd) if cwd else None,
                            env={"VOCO_URL": f"http://127.0.0.1:{self._port}"},
                        )
                    except PtyError as e:
                        raise ValueError(str(e)) from e
                    name = pp.handle
                else:
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
                self._schedule_state_save()
            out = {"backend": backend}
            out["term" if backend == "pty" else "tmux_session"] = name
            if wt_path is not None:
                out["worktree"] = wt_path
            return out
        if cmd == "session.kill":
            name = str(payload.get("name", ""))
            kill_error: str | None = None
            try:
                if name.startswith("pty-"):
                    from voco.adapters.ptyterm import PtyError

                    if self._pty is None:
                        raise ValueError(f"no such terminal: {name}")
                    try:
                        # akill: the terminate wait must not stall the loop.
                        await self._pty.akill(name)
                    except PtyError as e:
                        raise ValueError(str(e)) from e
                else:
                    mgr = self._tmux()
                    await self._run_blocking(
                        lambda: mgr.kill(name, host=payload.get("host"))
                    )
            except Exception as e:
                if name not in self._spawned_worktrees:
                    raise
                # The session already died (a natural exit, or an earlier
                # kill that KEPT a dirty worktree): the reap below is the
                # half that still matters — a dirty tree must stay
                # reclaimable by a later clean kill.
                kill_error = str(e)
            out = await self._reap_worktree(name)
            if kill_error is not None:
                out["session"] = f"already gone ({kill_error})"
            return out
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

            # A daemon-owned pty answers from its ring buffer — no tmux.
            name = str(payload.get("name", "")).strip()
            if name and not payload.get("target"):
                s = self.registry.by_call_name(name)
                pty_proc = self._pty_for(s) if s is not None else None
                if pty_proc is not None:
                    text = pty_proc.capture()
                    return {"text": text, "hint": classify(text)}
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
        if cmd == "workspace.live":
            key = str(payload.get("workspace", ""))
            if self.workspaces.get(key) is None:
                raise ValueError(f"unknown workspace: {key}")
            live = bool(payload.get("live", True))
            self._live_workspaces[key] = live
            return {"workspace": key, "live": live}
        if cmd == "workspace.open":
            return await self._workspace_open(payload)
        if cmd == "workspace.register":
            return self._workspace_register(payload)
        if cmd == "page.publish":
            return await self._page_publish(payload)
        if cmd == "workspace.link":
            return await self._workspace_link(payload)
        if cmd == "workspace.files":
            return await self._workspace_files(payload)
        if cmd == "attach.snippet":
            return self._attach_snippet(payload)
        if cmd == "session.transcript":
            name = str(payload.get("name", "")).strip()
            s = self.registry.by_call_name(name)
            if s is None:
                raise ValueError(f"no session named {name!r}")
            return self.registry.transcript(s.session_id)
        try:
            return handle_workbench_command(
                self.workspaces, cmd, payload, data_dir=self._workspace_data_dir()
            )
        except KeyError:
            pass  # not a workbench command; fall through
        raise ValueError(f"unknown command {cmd!r}")

    async def _workspace_open(self, payload: dict) -> dict:
        """DESIGN-DECK U0: mint a workspace from a checkout path on the
        daemon host — agentless review parity (the first-run 'open a
        repo' and the picker need a workspace before any agent exists)."""
        raw = str(payload.get("path", "")).strip()
        if not raw:
            raise ValueError("path required")
        path = Path(raw).expanduser()
        if not path.is_dir():
            raise ValueError(f"not a directory: {raw}")

        def probe() -> dict:
            import subprocess

            def git(*args: str) -> str | None:
                try:
                    out = subprocess.run(
                        ["git", "-C", str(path), *args],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    return None
                return out.stdout.strip() or None if out.returncode == 0 else None

            top = git("rev-parse", "--show-toplevel")
            if top is None:
                return {}
            return {
                "top": top,
                "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
                "common_dir": git(
                    "rev-parse", "--path-format=absolute", "--git-common-dir"
                ),
            }

        facts = await self._run_blocking(probe)
        if not facts:
            raise ValueError(f"not a git checkout: {raw}")
        import socket as _socket

        ws = self.workspaces.resolve(
            {
                "host": _socket.gethostname().split(".")[0],
                "cwd": facts["top"],
                "worktree": facts["top"],
                "repo": Path(facts["top"]).name,
                "branch": facts.get("branch"),
                "common_dir": facts.get("common_dir"),
            }
        )
        from voco.adapters.gitstatus import git_status

        self.workspaces.set_git(
            ws.key, await self._run_blocking(lambda: git_status(ws.root))
        )
        return {
            "workspace": ws.key,
            "root": ws.root,
            "repo": ws.repo,
            "branch": ws.branch,
        }

    def _workspace_register(self, payload: dict) -> dict:
        """Register derived workspace facts without creating an agent session."""
        identity = payload.get("identity")
        if not isinstance(identity, dict):
            raise ValueError("identity required")
        if not identity.get("host") or not (
            identity.get("worktree") or identity.get("cwd")
        ):
            raise ValueError("identity needs host and cwd/worktree")
        ws = self.workspaces.resolve(identity)
        return {
            "workspace": ws.key,
            "root": ws.root,
            "repo": ws.repo,
            "branch": ws.branch,
        }

    async def _page_publish(self, payload: dict) -> dict:
        """DESIGN-DECK U0: human-initiated diff publish — the §3.2
        sentence ('works with no agent attached') as a real command. Same
        resolver, same caps, same upsert as the agent bridge verb; no
        session required."""
        from voco.adapters.diffsource import DiffResolveError, source_ref
        from voco.core.diff import parse_diff
        from voco.server.workbench import MAX_DIFF_BYTES, diff_fingerprint

        key = str(payload.get("workspace", ""))
        ws = self.workspaces.get(key)
        if ws is None:
            raise ValueError(f"unknown workspace: {key}")
        if ws.kind == "sessionspace":
            raise ValueError(f"{key} has no checkout; review needs a workspace")
        type_ = payload.get("type") or ("diff" if "source" in payload else None)
        if type_ == "doc":
            from voco.core.limits import utf8_size
            from voco.server.workbench import MAX_DOC_BYTES, confined_read

            path = payload.get("path")
            content = payload.get("content")
            if path:
                import socket as _socket

                if ws.host != _socket.gethostname().split(".")[0]:
                    raise ValueError(
                        "remote workspace: push doc content instead of path"
                    )
                doc_path = (
                    str(Path(ws.root, str(path)).resolve())
                    if not Path(str(path)).is_absolute()
                    else str(path)
                )
                confined_read(ws.root, doc_path)
                page = self.workspaces.push_doc(
                    ws, name=payload.get("name"), path=doc_path
                )
            else:
                text = None if content is None else str(content)
                if text is not None and utf8_size(text) > MAX_DOC_BYTES:
                    raise ValueError(f"doc too large ({utf8_size(text)} bytes)")
                page = self.workspaces.push_doc(
                    ws, name=payload.get("name"), content=text
                )
            return {
                "ok": True,
                "page_id": page.page_id,
                "rev": page.rev,
                "workspace": ws.key,
                "root": ws.root,
            }
        if type_ != "diff":
            raise ValueError("type must be doc|diff")
        source = payload.get("source")
        if not isinstance(source, dict) or not (
            {"pr", "branch", "staged", "worktree"} & source.keys()
        ):
            raise ValueError(
                "source must be one of {pr}|{branch}|{staged: true}|{worktree: true}"
            )
        try:
            text = await self._run_blocking(
                lambda: self.bridge.diff_resolver.resolve(source, ws.root)
            )
        except DiffResolveError as e:
            raise ValueError(f"{e} (workspace root {ws.root!r})") from e
        from voco.core.limits import utf8_size

        if utf8_size(text) > MAX_DIFF_BYTES:
            raise ValueError(f"diff too large ({utf8_size(text)} bytes)")
        page = self.workspaces.upsert_diff(
            ws,
            ref=source_ref(source),
            title=source_ref(source),
            files=parse_diff(text),
            source=source,
            diff_key=diff_fingerprint(text),
        )
        return {
            "ok": True,
            "page_id": page.page_id,
            "rev": page.rev,
            "workspace": ws.key,
            "root": ws.root,
        }

    async def _workspace_link(self, payload: dict) -> dict:
        """DESIGN-DECK rev 5 (U2a): GitHub links on a workspace. Manual
        set/clear always wins — over detect in the SAME command and over
        an in-flight detect (xai B2/W3: detect fills only kinds untouched
        by this payload AND missing both before and after the gh call).
        Detected links carry src="gh" so a branch switch drops them (W4);
        the detect cache is keyed by branch for the same reason. gh is an
        OPTIONAL edge: silence on every failure."""
        key = str(payload.get("workspace", ""))
        ws = self.workspaces.get(key)
        if ws is None:
            raise ValueError(f"unknown workspace: {key}")
        kinds = self.workspaces.LINK_KINDS
        manual: dict[str, Any] = {}
        for k in kinds:
            if k not in payload:
                continue
            v = payload[k]
            # dicts get provenance; None clears; junk flows through so
            # set_links raises a contextual 400 (never silently dropped).
            manual[k] = {**v, "src": "manual"} if isinstance(v, dict) else v
        if manual:
            self.workspaces.set_links(key, manual)
        cache_key = f"{key}@{ws.branch}"
        wants_detect = (
            payload.get("detect")
            and ws.kind == "workspace"
            and ws.branch
            and (payload.get("force") or cache_key not in self._gh_checked)
        )
        if wants_detect:
            self._gh_checked.add(cache_key)
            fillable = [k for k in kinds if k not in manual and k not in ws.links]
            root, branch = ws.root, str(ws.branch)
            try:
                found = await self._run_blocking(
                    lambda: self._ghlink_detect(root, branch)
                )
            except Exception:
                # The optional-gh decision holds even for a misbehaving
                # detector: no link, never an error.
                found = None
            if found:
                fill = {
                    k: {**v, "src": "gh"}
                    for k, v in found.items()
                    if k in fillable and k not in ws.links
                }
                if fill:
                    self.workspaces.set_links(key, fill)
        return {"workspace": key, "links": ws.links}

    async def _workspace_files(self, payload: dict) -> dict:
        """B1c file viewer: the workspace's tracked files (git ls-files),
        capped — the client filters locally, the source view reads each
        file through the confined /v1/file route."""
        from voco.adapters.diffsource import _default_runner

        key = str(payload.get("workspace", ""))
        ws = self.workspaces.get(key)
        if ws is None:
            raise ValueError(f"unknown workspace: {key}")
        if ws.kind == "sessionspace":
            raise ValueError(f"{key} has no checkout; no tracked files")
        root = ws.root
        r = await self._run_blocking(lambda: _default_runner(["git", "ls-files"], root))
        if r.returncode != 0:
            raise ValueError(f"git ls-files failed in {root!r}: {r.stderr.strip()}")
        files = r.stdout.splitlines()
        cap = 5000
        return {
            "workspace": key,
            "files": files[:cap],
            "truncated": max(0, len(files) - cap),
        }

    def _attach_snippet(self, payload: dict) -> dict:
        """DESIGN-DECK rev 5 (U2d server half): the paste-ready attach
        story for the connect modal — MCP config + CLI fallback + ssh
        remote hint. Read-only; mirrors the CLI's attach-cmd output."""
        url = f"http://127.0.0.1:{self._port}"
        env = {"VOCO_URL": url}
        token = self.cfg.get("bridge", {}).get("token")
        if token:
            env["VOCO_TOKEN"] = str(token)
        return {
            "url": url,
            "mcp": {"mcpServers": {"voco": {"command": "voco-mcp", "env": env}}},
            "cli": 'fallback: agents run `voco say "..."` and `voco listen`',
            "remote": (
                f"~/.ssh/config on the agent host: RemoteForward {self._port} "
                f"localhost:{self._port}"
            ),
        }

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
        reason: str | None = None
        if key in self._HOT_APPLY:
            try:
                if key == "audio.duplex":
                    if self.voice is None:
                        raise ValueError("no voice loop running")
                    self._set_duplex(str(value))
                elif key == "audio.attention":
                    if self.voice is None:
                        raise ValueError("no voice loop running")
                    if not self.voice.set_attention(AttentionMode(str(value))):
                        raise ValueError(
                            f"refused: {self.voice.wake_unavailable_reason}"
                        )
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
            except Exception as e:
                applied = False  # persisted; a restart re-attempts it
                reason = str(e)
        resp = {
            "key": key,
            "value": value,
            "applied": applied,
            "restart_required": not applied,
            "written": str(config_mod.overrides_path(self._config_path)),
        }
        if reason:
            resp["reason"] = reason
        return resp

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
        """Config snapshot minus secrets (tokens, api keys). `_hot` names
        the keys that apply live — the settings modal marks the rest as
        restart-required IN ADVANCE (honest hot-apply, U3-pulled-forward)."""
        out: dict[str, Any] = {}
        for section, values in self.cfg.items():
            if not isinstance(values, dict):
                continue
            out[section] = {
                k: v for k, v in values.items() if "token" not in k and "key" not in k
            }
        out["_hot"] = sorted(self._HOT_APPLY)
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
        if result in {"no_session", "disconnected"}:
            raise ValueError("selected agent is disconnected; start its listener first")
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
        "input.drained",
        "agent.say",
        "screen.updated",
        "digest.updated",
    }

    def _dump_state(self) -> dict[str, Any]:
        data = self.registry.dump()
        data["spawned_worktrees"] = dict(self._spawned_worktrees)
        return data

    def _restore_state(self) -> None:
        try:
            self._state.acquire(wait_s=6.0)
            self._state_locked = True
        except Exception as e:
            self.bus.emit(
                "daemon.error",
                {"error": f"state lock: {e} — session persistence off"},
            )
            return
        data, err = self._state.load()
        if err:
            self.bus.emit("daemon.error", {"error": f"state: {err}"})
        if data:
            ttl = float(self.cfg.get("state", {}).get("session_ttl_s", 86400.0))
            n = self.registry.restore(data, max_age_s=max(0.0, ttl))
            owned = data.get("spawned_worktrees", {})
            if isinstance(owned, dict):
                self._spawned_worktrees = {
                    name: path
                    for name, path in owned.items()
                    if isinstance(name, str)
                    and bool(name)
                    and isinstance(path, str)
                    and Path(path).is_absolute()
                }
            if n:
                log.info("restored %d session(s) from %s", n, self._state.path)
            for s in self.registry.all():
                # Restored sessions get their workspaces back immediately,
                # same as a fresh register — the rail must not be empty
                # until agents happen to ping.
                self.workspaces.resolve(s.identity)

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
            # 6s grace: a restart routinely races the dying daemon's
            # shutdown flush; boot happens before the loop serves anything.
            self._manifest.acquire(wait_s=6.0)
            self._manifest_locked = True
        except Exception as e:
            # A live sibling daemon owns the data dir: run without durable
            # workbench state rather than clobber it (fail-soft, named).
            self.bus.emit(
                "daemon.error",
                {"error": f"workspace lock: {e} — workbench persistence off"},
            )
            return
        manifests, errors = self._manifest.load_all()
        for err in errors:
            self.bus.emit("daemon.error", {"error": f"manifest: {err}"})
        n = sum(1 for m in manifests if self.workspaces.restore_workspace(m))
        if n:
            log.info("restored %d workspace(s)", n)

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
            if s.connected
            and "review" in s.capabilities
            and self.workspaces.home_of(s.identity) is ws
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
        if (
            active is not None
            and active in here
            # An active session only wins the election while it is
            # actually reachable — parked (wakeable now) or recently
            # heard from. A stale corpse holding the active slot must
            # not swallow asks/wakes (live-test bug).
            and (active.parked or time.time() - active.last_seen < 600)
        ):
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
            if s is not None and s.connected and "review" in s.capabilities:
                return s
            return None  # its agent is gone/ineligible; ledger still has it
        return self._primary_session(ws)

    def _wire_terminal_pages(self) -> None:
        """W4: a registering session with a managed terminal gets its
        pinned `term:<call_name>` page — stream for daemon-owned ptys,
        read-only mirror for tmux panes. Cells ride the snapshot."""
        self.registry.term_cells = self._term_cells
        # §6 display state: a daemon pty's liveness feeds the "gone" dot.
        self.registry.handle_alive = lambda s: (
            pp.alive if (pp := self._pty_for(s)) is not None else None
        )

        def on_event(env) -> None:
            if env.type != "session.attached":
                return
            s = self.registry.get(str(env.payload.get("session_id") or ""))
            if s is None:
                return
            pp = self._pty_for(s)
            if pp is not None:
                self.workspaces.upsert_terminal(
                    s.identity,
                    session_id=s.session_id,
                    call_name=s.call_name,
                    mode="stream",
                    handle=pp.handle,
                )
            elif s.inject_target is not None:
                self.workspaces.upsert_terminal(
                    s.identity,
                    session_id=s.session_id,
                    call_name=s.call_name,
                    mode="mirror",
                )

        self.bus.subscribe(on_event)

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
        if not self._state_locked:
            return
        if self._state_save_task is not None and not self._state_save_task.done():
            return

        async def save_soon() -> None:
            await asyncio.sleep(0.5)
            data = self._dump_state()
            try:
                await self._run_blocking(lambda: self._state.save(data))
            except Exception as e:
                self.bus.emit("daemon.error", {"error": f"state save: {e}"})

        self._state_save_task = asyncio.get_running_loop().create_task(save_soon())

    # ---- live-git tracker (SPEC-WORKBENCH §11 W5) ----------------------------

    def _start_live_git(self, loop: asyncio.AbstractEventLoop) -> None:
        interval = float(self.cfg.get("workbench", {}).get("live_git_s", 5.0))
        if interval <= 0:
            return  # globally off
        self._live_git_task = loop.create_task(self._live_git_loop(interval))

    async def _live_git_loop(self, interval: float) -> None:
        """Diff pages TRACK the checkout instead of freezing at push: on an
        interval, re-resolve each diff from its RECORDED source and, when
        the content moved, upsert exactly like a re-push (rev bump,
        interdiff, stale findings). Conservative by design: transient git
        states (rebase, lock) and empty resolutions are skipped — a
        mid-rebase tree must never clobber the review."""
        import socket as _socket

        host = _socket.gethostname().split(".")[0]
        from voco.adapters.gitstatus import git_status

        while True:
            await asyncio.sleep(interval)
            await self._live_git_tick(host, git_status)

    async def _session_liveness_loop(self) -> None:
        """Publish listener-expiry transitions even when no other event fires."""
        while True:
            await asyncio.sleep(5.0)
            self.registry.refresh_liveness()

    async def _live_git_tick(self, host: str, git_status: Any) -> None:
        """Refresh local workspaces concurrently with a small hard bound."""
        raw_limit = self.cfg.get("workbench", {}).get("live_git_concurrency", 4)
        try:
            limit = max(1, min(16, int(raw_limit)))
        except (TypeError, ValueError):
            limit = 4
        semaphore = asyncio.Semaphore(limit)

        async def refresh_workspace(ws: Workspace) -> None:
            if not self._live_workspaces.get(ws.key, True):
                return  # workspace.live {live: false}
            if ws.host != host or ws.kind != "workspace":
                return  # a remote disk is not ours to resolve
            async with semaphore:
                # B1c: the rail's git facts ride the same tick; set_git
                # converges, so an unchanged status emits nothing.
                root = ws.root
                try:
                    st = await self._run_blocking(lambda r=root: git_status(r))
                except Exception as e:
                    self.bus.emit("daemon.error", {"error": f"live-git {ws.key}: {e}"})
                    return
                self.workspaces.set_git(ws.key, st)
                for page in list(ws.pages.values()):
                    if page.type != "diff" or page.closed:
                        continue
                    if not page.data.get("source"):
                        continue  # pasted text: nothing to re-resolve
                    try:
                        await self._live_refresh(ws, page)
                    except Exception as e:
                        self.bus.emit("daemon.error", {"error": f"live-git: {e}"})

        await asyncio.gather(*(refresh_workspace(ws) for ws in self.workspaces.all()))

    async def _live_refresh(self, ws: Workspace, page) -> None:
        from voco.core.diff import parse_diff
        from voco.server.workbench import (
            confined_read,
            diff_fingerprint,
        )

        source = page.data["source"]

        def resolve() -> str | None:
            try:
                if "diff_file" in source:
                    # Same confinement stance as the push route.
                    return confined_read(ws.root, str(source["diff_file"]))
                return self.bridge.diff_resolver.resolve(source, ws.root)
            except Exception:
                return None  # transient git state — try next tick

        text = await self._run_blocking(resolve)
        if not text:
            return
        from voco.core.limits import utf8_size
        from voco.server.workbench import MAX_DIFF_BYTES

        if utf8_size(text) > MAX_DIFF_BYTES:
            # An oversized diff would stall the loop every tick: stop
            # tracking THIS workspace and say why, once.
            self._live_workspaces[ws.key] = False
            self.bus.emit(
                "daemon.error",
                {
                    "error": f"live-git off for {ws.key}: diff exceeds "
                    f"{MAX_DIFF_BYTES} bytes (workspace.live re-enables)"
                },
            )
            return
        key = diff_fingerprint(text)
        if key == page.data.get("diff_key"):
            return
        files = parse_diff(text)
        if not files:
            return  # conservative: never clobber the review with empty
        self.workspaces.upsert_diff(
            ws,
            ref=page.ref,
            title=page.title,
            files=files,
            source=source,
            diff_key=key,
        )

    # ---- pane watcher (proactive eyes on unattended terminals) --------------------

    def _start_watcher(self, loop: asyncio.AbstractEventLoop) -> None:
        cfg = self.cfg.get("watcher", {})
        if not cfg.get("enabled", True):
            return
        from voco.watcher import PaneWatcher

        def pty_capture(s: Session) -> str | None:
            pp = self._pty_for(s)
            return pp.capture_tail() if pp is not None else None

        watcher = PaneWatcher(
            self.registry,
            self._tmux(),
            interval_s=float(cfg.get("interval_s", 3.0)),
            on_waiting=self._on_pane_waiting if cfg.get("speak", True) else None,
            pty_capture=pty_capture,
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
        """Every daemon.error event lands in the structured log (P4):
        one subscription is the single funnel, so emitters never also
        print — the deck gets the event, the log gets the line."""

        def on_event(env) -> None:
            if env.type == "daemon.error":
                log.error("%s", env.payload.get("error", "?"))

        self.bus.subscribe(on_event)

    def _health_info(self) -> dict[str, Any]:
        """Live facts for /v1/health — cheap, no locks, no I/O."""
        return {
            "version": VERSION,
            "uptime_s": round(time.monotonic() - self._started_mono, 1),
            "port": self._port,
            "voice": self.voice is not None,
            "floor_managed": self._floor is not None,
            "floor_restarts": self._floor.restarts if self._floor else 0,
        }

    # ---- agent says become speech --------------------------------------------------

    def _wire_say_speech(self) -> None:
        def on_event(env) -> None:
            if env.type != "agent.say" or not env.payload.get("active"):
                return
            if self.voice is not None:
                s = self.registry.get(str(env.payload.get("session_id") or ""))
                self.voice.speak_agent(
                    env.payload["text"],
                    env.payload.get("turn_id"),
                    who=s.call_name if s is not None else None,
                )

        self.bus.subscribe(on_event)

    async def _maybe_start_floor(self) -> None:
        """P3: supervise the bundled TTS floor when the config points at
        it (decision: floor_supervisor.should_manage — loopback:8880 by
        default, manage_floor overrides; foreign engines never touched)."""
        from voco.adapters.floor_supervisor import (
            FloorSupervisor,
            floor_argv,
            should_manage,
        )

        port = should_manage(self.cfg.get("tts") or {})
        if port is None:
            return
        self._floor = FloorSupervisor(floor_argv(port), emit=self.bus.emit)
        await self._floor.start()
        log.info("tts floor managed on 127.0.0.1:%d", port)

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
        self._wire_terminal_pages()
        self._start_live_git(loop)
        self._start_watcher(loop)
        self._session_liveness_task = loop.create_task(self._session_liveness_loop())
        try:
            runner = await run_server(self.bridge, host=host, port=port)
        except OSError as e:
            raise SystemExit(
                f"voco-d: cannot bind {host}:{port} ({e.strerror or e}); "
                "is another voco-d running?"
            ) from e
        if not self.no_audio:
            from voco import assets
            from voco.voice_loop import VoiceLoop

            try:
                # P2: the VAD model must exist BEFORE the loop builds —
                # configured paths resolve against the config file's dir
                # (never the cwd), the default downloads the pinned asset.
                audio_cfg = self.cfg.setdefault("audio", {})
                audio_cfg["silero_model"] = str(
                    assets.ensure_silero(
                        audio_cfg.get("silero_model"),
                        config_dir=self._config_path.parent,
                        log=logging.getLogger("voco.assets").info,
                    )
                )
                self.voice = VoiceLoop(self.cfg, self.bus, host=self)
                await self.voice.start(loop)
            except Exception as e:
                self.voice = None
                # one funnel: the wired error log turns this into the
                # ERROR line; the deck gets the same event (P4)
                self.bus.emit(
                    "daemon.error",
                    {"error": f"voice loop unavailable ({e}) — running headless"},
                )
            await self._maybe_start_floor()
        log.info(
            "voco-d %s listening on %s:%d%s",
            VERSION,
            host,
            port,
            " (no audio)" if self.voice is None else "",
        )
        try:
            await stop.wait()
        finally:
            # Order matters: unpark agents first (they exit their listen
            # cleanly), then close the server, then the audio shell.
            await self.bridge.shutdown()
            await runner.cleanup()
            lifecycle_tasks = [
                task
                for task in (
                    self._watcher_task,
                    self._session_liveness_task,
                    self._live_git_task,
                    self._state_save_task,
                    self._manifest_save_task,
                    *self._background_tasks,
                )
                if task is not None
            ]
            for task in lifecycle_tasks:
                task.cancel()
            if lifecycle_tasks:
                await asyncio.gather(*lifecycle_tasks, return_exceptions=True)
            self._background_tasks.clear()
            if self._manifest_locked:
                # Server closed + mutation tasks cancelled: nothing can
                # touch the store anymore — flush and RELEASE THE LOCK
                # NOW, before the audio/model teardown below (it can take
                # >10s, and a restarting daemon is waiting on this lock —
                # the race cost us persistence four times on 2026-07-08).
                self._dirty_workspaces.update(self.workspaces.dirty_keys())
                self._flush_manifests()
                self._manifest.release()
            if self._floor is not None:
                await self._floor.stop()  # the floor dies with its daemon
            if self.voice is not None:
                await self.voice.aclose()
            if self._state_locked:
                try:
                    self._state.save(self._dump_state())  # final synchronous save
                except Exception as e:
                    log.error("state save failed: %s", e)
                finally:
                    self._state.release()
                    self._state_locked = False
            if self._pty is not None:
                # v1: pty terminals die with the daemon (§5, honest).
                self._pty.shutdown()
            log.info("shut down cleanly")


def main() -> None:
    parser = argparse.ArgumentParser(prog="voco-d")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--port", type=int, default=7777)
    parser.add_argument("--no-audio", action="store_true")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="DEBUG-level logging"
    )
    args = parser.parse_args()
    # Logging first (P4): config warnings and every later line get
    # levels, timestamps, and the rotating file in the state dir.
    from voco import logsetup

    log_file = logsetup.setup(verbose=args.verbose)
    if log_file is not None:
        log.debug("logging to %s", log_file)
    cfg = load_config(args.config)
    daemon = Daemon(cfg, no_audio=args.no_audio, config_path=args.config)
    try:
        asyncio.run(daemon.run(port=args.port))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
