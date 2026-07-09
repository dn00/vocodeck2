# BUILD-PROD — the production-readiness campaign (plan + journal)

Goal: voco installs from a wheel on a clean machine and runs as a
managed, diagnosable, hardened local service — "production ready" for
a local-first voice control plane (ADR-0002 Tier 0/1 posture, minus
the Tauri shell which stays deferred).

Ground rules (best practice, non-negotiable):
- Every slice: gates green (pytest · mypy · ruff+format · tsc ·
  protocol drift) + a LIVE verification of the actual behavior
  (process really restarts, model really downloads, log really
  rotates) before its commit. Green gates alone have lied before.
- Server behavior changes ship with tests; new subsystems ship with
  their own test file. No silent scope trims — deviations are
  journaled and surfaced.
- Reversibility: any pinned decision may be reversed for a solidly
  better product — say so loudly in the journal + tell the captain.
- Honest signals everywhere: no fake states, no dead affordances,
  degraded modes announce themselves.

## The audit (2026-07-09) — what production-ready means here

### A · Ship-blockers
- [ ] **P1 — lifecycle ownership**: `voco up` / `voco down` /
      `voco status` / `voco logs`; pidfile + health check; idempotent
      up; launchd agent install/uninstall (macOS first, systemd unit
      documented later); XDG default config discovery
      (~/.config/voco/config.toml) so a service needs no flags.
- [ ] **P2 — assets that survive leaving the repo**: absolute model
      defaults under the cache dir; first-run downloads (silero VAD,
      whisper via faster-whisper, kokoro voices) with progress +
      checksums into VOCO_CACHE; `models/silero_vad.onnx` relative
      default is a bug outside the repo.
- [ ] **P3 — TTS floor lifecycle**: voco-d supervises voco-tts-floor
      (spawn, health, restart, shutdown) — no more hand-run stale
      floor processes; fix the port mismatch ([tts].base_url default
      :8880 vs the floor's :8000).
- [ ] **P4 — real logging**: structured logs (levels, timestamps),
      rotating file in the state dir, --verbose; daemon.error events
      consistently surfaced; the Input-Monitoring/PTT warning must
      reach the deck, not die in stderr.
- [ ] **P5 — `voco doctor`**: mic permission, Input Monitoring grant,
      models present, ports free, audio devices, state-dir health —
      actionable output.

### B · Hardening
- [ ] **P6 — audio device churn**: mic unplug/default-change mid-run →
      re-open loop + honest event.
- [ ] **P7 — STT failure modes**: load/device failures → announced
      degraded mode; backpressure when transcription lags speech.
- [ ] **P8 — auth posture**: refuse non-loopback bind without a bridge
      token; document the remote/TLS story (reverse proxy).
- [ ] **P9 — state integrity**: schema-version fields in
      registry/manifests, migration seam, corrupt-file quarantine
      (rename + fresh + loud log); atomic writes audited.
- [ ] **P10 — retention**: prune closed-page revs / resolved findings
      by policy; state-dir size in `voco status`.
- [ ] **P11 — the stream-stall ghost**: soak test (long synthetic
      session) to reproduce or retire the old handoff bug.
- [ ] **P12 — wake-word honesty**: "wake" attention mode gated on the
      detector actually being installed/loaded.
- [ ] **P13 — multi-writer races**: two decks + MCP + CLI on
      mic.set/ptt.* concurrently; verify or serialize.

### C · Features owed for daily production
- [ ] **P14 — OS notifications** on blocked / needs-you / ask-answered.
- [ ] **P15 — agent-integration one-pager**: the bridge contract
      (listen → pending review → reply/status) documented for any
      agent harness; live ask-answer round-trip verified.
- [ ] **P16 — `voco config get/set`** CLI parity, validate-on-write.

### D · Release & docs
- [ ] **P17 — README quickstart truth-check** against a wheel install.
- [ ] **P18 — versioning**: tags + changelog; version in `voco status`
      and the deck.
- [ ] **P19 — CI packaging job**: build wheel → install → import/run
      smoke on 3 OSes. (+ the deck smoke test, paused by captain.)

Deferred (unchanged): Tauri shell · streaming STT captions ·
post-review-to-PR · settings polish · light theme · full palette.

Order: P1 → P2+P3 → P4+P5 → B by risk → C → D. UI work is paused.

## Journal

- **2026-07-09 · P1 SHIPPED — lifecycle ownership.** New
  clients/voco_cli/lifecycle.py (pure helpers split from I/O for
  testability) + `voco up|down|logs|autostart` wired into the CLI
  (they run without a session — must work while the daemon is down).
  `up`: health-probe first (idempotent), spawn voco-d detached
  (start_new_session, output → managed log with a dated banner),
  pidfile, bounded health wait, log-tail on failure; prefers the
  venv's voco-d, falls back to `python -m voco.daemon` for source
  checkouts. `down`: pidfile → SIGTERM → 15s wait → loud SIGKILL last
  resort; refuses to guess about daemons it didn't start (points at
  launchd/terminal instead). `logs`: tail + -f follow. `autostart`:
  launchd agent (RunAtLoad; KeepAlive.SuccessfulExit=false so crashes
  restart but `voco down`/clean exits stick; bootout-then-bootstrap =
  idempotent reinstall); non-macOS prints a systemd --user unit as
  guidance. Lifecycle files live in the DEFAULT state dir
  (~/.local/state/voco, $VOCO_STATE_DIR override — added for hermetic
  testing) because pidfile/log are per-machine service facts, not
  per-config state. XDG config discovery already existed
  (~/.config/voco/config.toml) — audit assumption corrected. Tests:
  6 new (argv shapes, env override, garbage-pidfile-is-stale,
  pid_alive, plist contract incl. crash-restart semantics, systemd
  unit). LIVE drill on :7913 hermetic: up → healthy; second up →
  "already running"; logs (incidentally proving the workspace-lock
  guard: my scratch config shared voco-wb with the :7911 verify
  daemon and persistence refused correctly); down → stopped; double
  down → clean no-op; port freed. autostart exercised read-only
  (status) — install/uninstall touches the captain's login items, so
  the live install drill is deferred to the captain's first real use.
  Gates: 380 pytest · mypy · ruff+format. NEXT: P2 (assets) + P3
  (TTS floor supervision).
