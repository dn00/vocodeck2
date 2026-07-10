# BUILD-PROD — the production-readiness campaign (plan + journal)

Goal: voco installs from a wheel on a clean machine and runs as a
managed, diagnosable, hardened local service — "production ready" for
a local-first voice control plane (ADR-0002 Tier 0/1 posture, minus
the Tauri shell which stays deferred).

Ground rules (best practice, non-negotiable):
- **Every phase ends with an /xai adversarial review** (Codex,
  hostile mode) BEFORE its commit; blockers fixed in the same slice,
  deferrals journaled with reasons. (Captain's standing order,
  2026-07-09 — P1's review found 5 real blockers incl. a plist
  injection and a PID-reuse kill hazard.)
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
- [x] **P1 — lifecycle ownership**: `voco up` / `voco down` /
      `voco status` / `voco logs`; pidfile + health check; idempotent
      up; launchd agent install/uninstall (macOS first, systemd unit
      documented later); XDG default config discovery
      (~/.config/voco/config.toml) so a service needs no flags.
- [x] **P2 — assets that survive leaving the repo**: absolute model
      defaults under the cache dir; first-run downloads (silero VAD,
      whisper via faster-whisper, kokoro voices) with progress +
      checksums into VOCO_CACHE; `models/silero_vad.onnx` relative
      default is a bug outside the repo.
- [x] **P3 — TTS floor lifecycle**: voco-d supervises voco-tts-floor
      (spawn, health, restart, shutdown) — no more hand-run stale
      floor processes; fix the port mismatch ([tts].base_url default
      :8880 vs the floor's :8000).
- [x] **P4 — real logging**: structured logs (levels, timestamps),
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

- **2026-07-09 · P4 SHIPPED — real logging + /v1/health + honest PTT
  permission (xai round included).** New `voco.logsetup`: one setup for
  the `voco.*` tree — RotatingFileHandler on `<state-dir>/daemon.log`
  (5MB × 3, $VOCO_STATE_DIR honored) + stderr mirror, `VOCO_LOG_CONSOLE=0`
  for managed spawns, idempotent re-setup, uncreatable-dir degrade to
  stderr-only; its `_SafeFormatter` pins one-record-one-line (newlines
  escaped, C0/ESC stripped) because floor output and daemon.error
  payloads are untrusted text in a log a terminal will print. Daemon:
  every print() → logger, `--verbose`, logging wired before load_config,
  `_wire_error_log` is the SINGLE daemon.error→ERROR funnel (emitters
  never also print). NEW unauthenticated `GET /v1/health` (loopback bind,
  no mutation) returns the voco-d signature merged with live facts
  (version/uptime/port/voice/floor); the signature keys outrank the
  callback. Two log files by design: `daemon.log` (the daemon's own,
  rotating) vs `daemon.out` (spawn capture, pre-logging crash net —
  launchd/`voco up`/systemd all point raw output there with the mirror
  off). `voco logs -f` uses rotation-aware `follow_lines` (inode change /
  truncation → reopen; missing file — first boot or mid-rotation — is a
  wait, and a waited-for file is read from its first line). Floor
  supervisor pumps merged stdout+stderr line-by-line through the
  `voco.floor` logger (1MB line limit, truncated not fatal, pump reaped
  on both stop paths). Assets error taxonomy names server/network/
  timeout/mid-stream/disk failures distinctly. macOS Input Monitoring
  preflight (`CGPreflightListenEventAccess`, tri-state — None is never
  denied) makes the silent pynput PTT failure an actionable daemon.error;
  the deck shows daemon.error as sticky toasts deduped per message.

  **The /xai round (12 findings) — fixed in-slice:** asset temp files
  now unlink-then-`O_EXCL|O_NOFOLLOW` so crash debris under a recycled
  pid or a planted symlink can never become the published inode; cache-
  dir mkdir joined the AssetError taxonomy; URLError-wrapped connect
  timeouts say "timed out", not "offline"; `healthy()` hardened — the
  endpoint answering is authoritative (garbage JSON, wrong service, or
  error status = hard no; only 404/405 falls back to the P1 heuristic);
  `logs -f` no longer crashes on a not-yet-born daemon.log; `/v1/health`
  signature keys can't be shadowed by the info callback; the launchd
  plist and systemd guidance propagate a custom $VOCO_STATE_DIR so the
  daemon resolves the same state dir its lifecycle files live in.
  **Contested/accepted with reasons:** the floor `stop()` cancel "race"
  blocker — both stop paths already `_reap_pump`, race pinned by a
  stop-immediately-after-start test; the residual cancel-mid-spawn
  orphan window is microsecond-narrow pre-existing P3 shape, journaled
  not fixed. Oversized-line recovery logging one truncated record per
  64KB chunk is accepted: rotation bounds any child flood, and a
  newline-spamming child floods identically. Mid-run file-handler emit
  failures ride stdlib handleError to stderr (logging must never stop
  the daemon); deep degrade is a P5/doctor candidate. /v1/health info
  exposure accepted: loopback-only, and it reveals what the deck shows.

  Gates: 432 pytest (36 new) · mypy · ruff+format · tsc · protocol
  drift clean. LIVE (hermetic :7913, scratch VOCO_STATE_DIR/VOCO_CACHE
  AND scratch `[state].dir`): structured boot/error/shutdown lines in
  daemon.log, /v1/health JSON with merged facts, up idempotent → down
  clean → port freed, `logs -f` followed across a forced rename+fresh
  rotation live, daemon.out stayed banner-only. Live-drill lesson
  re-learned the hard way: $VOCO_STATE_DIR does NOT scope the session
  registry — that's config `[state].dir`, and a scratch config without
  it restores the REAL registry (first attempt did; killed the daemon
  pre-save, no pollution, config fixed). `voco doctor` (P5) should
  probably warn when lifecycle state dir and `[state].dir` diverge.
  NEXT: P5 (`voco doctor`).

- **2026-07-09 · P2+P3 SHIPPED — assets + TTS floor supervision (xai
  round included).** P2: new voco.assets — pinned model downloads
  (silero VAD pinned to snakers4/silero-vad@b163605, the EXACT master
  bytes the local VAD tuning runs on — provenance hunted by hashing
  releases: v5.1.2/v5.1/v4.0 all ship different bytes; kokoro model +
  voices pinned to the model-files-v1.0 release, hashes taken from the
  captain's proven local files). Downloads stream to per-process temp
  files, fsync, verify the ON-DISK bytes, then atomically publish —
  the xai BLOCKER was two daemons sharing one .part inode, where a
  peer could keep writing through its open fd AFTER a verified rename
  (published file mutates post-verification). Configured paths resolve
  against the CONFIG FILE's dir (the relative `models/…` default only
  worked from the repo root); explicit-but-missing paths error rather
  than silently fall back. Daemon resolves the VAD model before
  VoiceLoop builds (failure → the existing honest headless path);
  tts_floor's bare urlretrieve (no checksum, torn-download loadable,
  relative default) replaced by assets. LIVE: real download from the
  pinned URL into a fresh cache, hash True; captain's real config
  resolves unchanged. P3: FloorSupervisor — voco-d spawns/supervises
  voco-tts-floor (same-venv argv), crash restarts with capped backoff
  (healthy-hour reset, >= boundary per review), TRANSIENT spawn
  failures retry with a 5-strike terminal give-up (EMFILE can heal;
  a broken install cannot), clean stop on daemon shutdown; decision is
  the pure should_manage(): loopback:8880 by default (voice_loop's
  dead :8000 default unified to 8880, doctor probe too),
  [tts].manage_floor overrides, remote engines never touched.
  Supervisor tests spawn real child processes (stop, crash-restart,
  give-up). Deferred with reasons: richer download-error taxonomy +
  log rotation (P4), doctor deep-verify of cached assets (P5), Windows
  process-tree/replace semantics (POSIX-first posture, journaled).
  Gates: 396 pytest · mypy · ruff+format. Captain adoption note: on
  next daemon restart the floor becomes managed IF tts.base_url is
  loopback:8880 — the hand-run July-3 floor process should be killed
  once (`pkill -f voco-tts-floor`) so the daemon owns it.

- **2026-07-09 · P1 HARDENED — the /xai round (now standing policy:
  every phase gets one).** Codex adversarial review found 5 real
  blockers; all fixed same-slice: (1) plist built via f-string XML was
  an INJECTION (config paths with XML metacharacters) → plistlib +
  atomic tmp-replace write, injection round-trip test; (2) PID reuse
  could SIGKILL an innocent process → pidfile pids are only trusted
  after `ps` confirms a voco-d identity (token-basename match — the
  first substring version let `vim voco-design.md` pass [unit test
  caught it], the second argv0-only version disowned our own daemon
  because console-script shims run as `python …/voco-d` [LIVE drill
  caught it — gates alone would have shipped both]); (3) PermissionError
  on kill now degrades honestly instead of tracebacking; (4) concurrent
  `voco up` double-spawn → exclusive spawn lock (O_EXCL, 60s stale
  expiry); (5) stale-pidfile deadlock → identity-based staleness with
  one more live-drill lesson folded in: an UNKNOWN probe (ps itself
  failing) keeps the pidfile — a transient hiccup must never orphan a
  healthy daemon (the pre-fix version deleted the record and stranded
  a live daemon). Also from the review: up failure paths clean the
  pidfile; health requires a voco-signed body (random 200 ≠ daemon;
  /v1/health endpoint queued for P4); lifecycle URLs are always local
  — VOCO_URL aims clients, never signals (down gained --port); systemd
  guidance shlex-quotes; launchd bootout errors surface on uninstall
  of a loaded job; seek-based log tail; --wait clamped; Windows gets
  an honest unsupported message. Deferred to P4 with reasons: log
  rotation + rotation-aware `logs -f` + real health endpoint. Tests
  9 (383 total); full live cycle re-drilled clean (up → managed stop →
  port freed). Ground rules updated: /xai every phase.

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
