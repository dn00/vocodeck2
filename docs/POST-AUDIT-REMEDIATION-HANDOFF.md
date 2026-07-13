# Post-audit production remediation handoff

- Date: 2026-07-13
- Target run: `pilot-vocodeck2-full`
- Repository: `/home/denk/code/vocodeck2-nightshift`
- Branch: `fix/vocodeck-production-hardening`

## Mission and boundaries

Finish the post-audit hardening work without regressing the already-verified
review surfaces, MCP bridge, browser workbench, persistence, or routing.

This is a local-first product. Do not expand this pass into First Mate feature
work, native desktop UI, remote multi-user authentication, or a redesign of the
agent protocol. First Mate can remain optional. Ordinary agent speech, review
pages/diffs/findings/asks, MCP, and the browser workbench are the release focus.

Work on the Nightshift machine, not directly through GitHub. Before every write,
verify all three of these:

```bash
pwd
git rev-parse --show-toplevel
git branch --show-current
```

The expected top level is `/home/denk/code/vocodeck2-nightshift`. Stop if the
workspace resolves to `firstmate-personal` or any other repository.

## Rollback points and current state

The last fully clean, fully gated production-hardening checkpoint is:

- Commit: `7d92d2083f100c9623c83dab5dc0781c596e678d`
- Tag: `checkpoint/production-hardening-20260712`
- Earlier baseline: `checkpoint/workbench-before-prod-hardening-20260712`

This document and the verified phase 1 work are saved together at the handoff
checkpoint `checkpoint/post-audit-handoff-20260713`. This is a WIP checkpoint,
not the final production-ready tag.

During handoff preparation the phase 1 diff consisted of:

```text
 M src/voco/adapters/speaker.py
 M src/voco/voice_loop.py
?? tests/test_speaker.py
```

They are part of the handoff checkpoint. If the next agent sees additional
working-tree changes, inspect and preserve them; never reset them blindly.

Verification completed for the phase 1 checkpoint work:

```text
uv run pytest -q tests/test_speaker.py       -> 2 passed
uv run ruff check <phase-1 files>            -> passed
uv run mypy                                  -> 58 source files passed
git diff --check                             -> passed
```

The complete suite has **not** been rerun after phase 1. Run the targeted voice
set before considering phase 1 complete:

```bash
set -e
uv run pytest -q tests/test_speaker.py tests/test_arbitration.py tests/test_voice_loop.py
uv run ruff check src/voco/adapters/speaker.py src/voco/voice_loop.py tests/test_speaker.py
uv run mypy
git diff --check
```

## What the previous hardening pass already fixed

Do not reopen or redesign these unless a new regression test proves a problem:

- Late First Mate output no longer reroutes an already-dispatched command.
- Late actions are turn/epoch guarded and stale callbacks are invalidated.
- Manifest keys are injective and safe across slash, percent, colon, and
  Windows backslash cases.
- Page-less findings and unanswered asks round-trip across restarts.
- Manifest corruption is quarantined and persistence uses durable replacement,
  file/directory fsync, and multi-daemon locks.
- Restored sessions are disconnected and cannot become active routing ghosts.
- HTTP and WebSocket control mutations share one serialization lock.
- WebSocket event and command queues are bounded; event loss closes with 1013
  and reconnect performs an authoritative snapshot.
- STT speculative work is bounded and stale results cannot win.
- Worktree ownership survives daemon restart.
- Browser origin checks, workbench tokens, error redaction, file confinement,
  HTML sandboxing, and loopback-only binding passed review.

## Phase 1 — finish the playback ownership fix (P1)

### Confirmed failure

`SpeakerPlayer.stop()` used to cancel the old task and set `self._task = None`.
Arbitration could then synchronously start a replacement. When the cancelled
task later unwound, its `finally` block saw a non-null `self._task`, cleared the
replacement, and called `on_finished()` for the wrong item. The replacement
kept playing but was no longer tracked by either the player or arbitration.

The audit reproduced the old behavior directly:

```text
player_tracks_second=False
queue_tracks_second=False
second_task_still_running=True
```

This is not only a First Mate problem. Any stop/preempt/start sequence can
corrupt ordinary agent playback ordering.

### Current checkpoint implementation

`src/voco/adapters/speaker.py` now:

1. Captures the task being stopped.
2. Relinquishes `self._task` ownership before delivering cancellation.
3. Emits the synchronous `playing=False` edge during stop.
4. Uses `asyncio.current_task()` in `_run()` and only clears/calls
   `on_finished()` when the finishing task still owns `self._task`.
5. Accepts an `on_error(Exception)` callback instead of silently swallowing
   playback failures.

`src/voco/voice_loop.py` wires playback errors to `daemon.error`.

`tests/test_speaker.py` contains:

- a real async cancellation/preemption regression test; and
- a playback failure observability/queue-advance test.

### Acceptance criteria

- A cancelled task cannot clear or finish a newer task.
- Stop without replacement produces exactly one `playing=False` edge.
- Preemption produces `[True, False, True, False]`, not a stale false edge from
  the cancelled task after the replacement starts.
- Natural completion advances arbitration exactly once.
- Device/TTS stream failure emits `daemon.error` and advances the queue.
- The targeted voice tests above pass under `set -e`.

Review the diff before committing. If it remains correct, commit this phase
separately so it can be reverted independently.

## Phase 2 — bound queued inputs (P2)

### Problem

`Session.queued` is an unbounded list in `src/voco/core/registry.py`. A dead or
busy agent plus repeated voice/browser/MCP input grows memory and the registry
state file indefinitely. `say_log` and `input_log` are already capped at 50;
queued commands are not.

### Required policy

Never silently discard a command. Reject the **new** command explicitly when
the target queue is full. Old queued commands must remain in their original
order. A production control plane must not pretend a command was accepted.

Suggested constants in `src/voco/core/registry.py` or a small shared limits
module:

```python
MAX_QUEUED_INPUTS = 100
MAX_INPUT_BYTES = 64 * 1024
```

Use UTF-8 byte size, not Python character count:

```python
len(text.encode("utf-8"))
```

### Implementation steps

1. In `Registry.dispatch()`, preserve the live-delivery path: if an agent is
   parked and delivery succeeds, do not apply the queue-count limit.
2. Before `s.queued.append(...)`, reject text above `MAX_INPUT_BYTES` and reject
   when `len(s.queued) >= MAX_QUEUED_INPUTS`.
3. Raise `ValueError` with a stable human-readable message. This integrates
   with existing paths:
   - typed/browser `say_as_user` becomes a 400 command reply;
   - voice dispatch is caught by `VoiceLoop._route_turn()` and surfaces as
     `daemon.error` instead of an unhandled task.
4. Do not append to `input_log` and do not emit `input.queued` for a rejected
   input.
5. On registry restore, defensively retain at most the newest
   `MAX_QUEUED_INPUTS` valid items. The runtime must not rehydrate an unlimited
   legacy/corrupt queue. Validate individual item text byte size as well.
6. Document the limit near the queue field and in the operational docs if they
   enumerate retention policies.

### Tests

Add tests in `tests/test_input_meta.py` or `tests/test_state.py` proving:

- exactly `MAX_QUEUED_INPUTS` can queue;
- the next input raises and is absent from queue/input history/events;
- a parked live delivery still succeeds even if an old queue is at its cap;
- oversize UTF-8 input is rejected by bytes, including multibyte text;
- restore truncates an oversized legacy queue deterministically;
- dump/restore preserves an in-limit queue.

## Phase 3 — bound screen content atomically (P2)

### Problem

Both `Registry.set_screen()` and `WorkspaceStore.upsert_screen()` append without
a limit. Screen content is duplicated in live registry state and the persisted
workspace page, and every `screen.updated` event carries the full content.
Repeated append can therefore amplify memory, disk, snapshot, WebSocket, and DOM
cost.

### Required policy

Use a shared UTF-8 limit and reject an update before mutating either copy:

```python
MAX_SCREEN_BYTES = 2 * 1024 * 1024
```

`show` may replace an old large screen with a valid smaller one. `append` must
calculate the exact candidate including the inserted newline. Never truncate
Markdown silently because that can corrupt code fences and review anchors.

### Implementation steps

1. Put `MAX_SCREEN_BYTES`, `utf8_size()`, and a pure candidate builder in a
   dependency-neutral module such as `src/voco/core/limits.py`.
2. In `BridgeServer._screen()`, validate the candidate before calling either
   `Registry.set_screen()` or `WorkspaceStore.upsert_screen()`.
3. Also enforce the limit inside both domain methods so non-HTTP callers cannot
   bypass it.
4. Convert the limit error to HTTP 413 (`HTTPRequestEntityTooLarge`) at the
   bridge boundary. MCP/CLI should receive a concise error, not a 500.
5. Ensure a rejected update changes neither the session screen nor the pinned
   screen page and emits neither `screen.updated` nor `page.updated`.
6. Apply the same cap defensively during registry/workspace restore. A single
   oversized stored screen/page should be dropped or clipped only according to
   an explicit documented recovery policy; prefer dropping the oversized
   screen content with a startup diagnostic over silently creating malformed
   Markdown.

### Tests

Add domain and HTTP tests proving:

- `show` at the exact byte limit succeeds;
- one byte over fails;
- append accounts for the newline and multibyte UTF-8;
- failure is atomic across registry and workspace page copies;
- HTTP returns 413 rather than 500;
- a rejected update emits no event;
- valid screen state still round-trips after restart.

## Phase 4 — align the HTTP request ceiling with page limits (P2)

### Confirmed mismatch

`BridgeServer.build_app()` currently creates `web.Application(...)` without a
`client_max_size`. With the locked aiohttp version its actual limit is
1,048,576 bytes. The workbench advertises/implements 2 MiB document content and
8 MiB pasted diffs. Large valid review payloads therefore fail in aiohttp
before VocoDeck's own validators run.

### Implementation steps

1. Define a request-envelope limit large enough for an 8 MiB diff after JSON
   escaping. A 16 MiB ceiling is a reasonable explicit value:

   ```python
   MAX_REQUEST_BYTES = 16 * 1024 * 1024
   ```

2. Construct the app with:

   ```python
   web.Application(
       middlewares=[error_middleware],
       client_max_size=MAX_REQUEST_BYTES,
   )
   ```

3. Keep the lower per-domain limits (`MAX_DOC_BYTES`, `MAX_DIFF_BYTES`, and
   `MAX_SCREEN_BYTES`). The application ceiling is only an outer DoS bound.
4. Audit every existing `len(text)` size check in `workbench.py` and `daemon.py`.
   Constants named `*_BYTES` must compare `len(text.encode("utf-8"))`.
5. Return structured 413 errors for domain-limit failures. Do not expose stack
   traces or filesystem paths.

### Tests

- Assert the built app has the explicit 16 MiB ceiling.
- POST a virtual document slightly above 1 MiB but below 2 MiB and prove it
  reaches the VocoDeck handler successfully.
- POST content above the 2 MiB document limit and expect 413.
- Exercise multibyte content so character count cannot bypass the byte cap.
- Keep an outer-ceiling test if aiohttp's test client makes it inexpensive.

## Phase 5 — make slow browser command outcomes explicit (P2)

### Problem

`src/voco/server/static/bus.mjs` rejects every command after 15 seconds, but the
server continues the serialized mutation. Slow worktree/session/SSH operations
can complete after the UI reports `timeout`. A user retry can therefore create a
duplicate operation.

### Minimum acceptable fix for this release

1. Do not present the timeout as a normal retryable failure. Introduce an
   `OutcomeUnknownError` (or equivalent error code) whose message says the
   operation may still complete and must be reconciled before retry.
2. Increase the client deadline to a documented value suitable for managed
   lifecycle commands (for example 60 seconds), while retaining an upper bound.
3. On outcome-unknown, force a socket reconnect/resnapshot and keep the initiating
   control disabled until the authoritative snapshot arrives.
4. Never automatically resend a mutation after timeout or disconnect.
5. Add a browser unit test with a deliberately delayed server reply. Prove the
   promise becomes outcome-unknown, the command is sent once, and the store
   resnapshots before another mutation is allowed.

### Preferred stronger follow-up

Add idempotency keys for long-running mutations and a bounded server-side
completed/in-flight result cache. A reconnect retry using the same key should
join the existing operation or return its cached result. Do not key solely by
the current `c1`, `c2` identifiers: those collide across tabs and reconnects.
Use a per-tab random client id plus monotonic operation id, bound the cache, and
test two tabs explicitly. This is stronger but may be deferred if the minimum
outcome-unknown behavior ships and the UI never auto-retries.

## Phase 6 — small lifecycle and release fixes

### Await terminal WebSocket sender cancellation (P3)

In `src/voco/server/workbench.py::term_ws`, the `finally` block currently calls
`sender.cancel()` without awaiting it. Change cleanup to:

```python
finally:
    pp.unsubscribe(queue)
    sender.cancel()
    await asyncio.gather(sender, return_exceptions=True)
```

Add or extend `tests/test_term_ws.py` to connect/disconnect repeatedly under
asyncio debug mode and prove there are no leaked pending tasks or unobserved
cancellations.

### Remove the hard-coded wheel version from CI (P3)

`.github/workflows/ci.yml` hard-codes
`dist/voco-0.0.1-py3-none-any.whl` twice. A version bump will break release
verification even when the build is correct.

Use a Bash block that asserts exactly one wheel exists and then installs it:

```yaml
- name: Install built wheel
  shell: bash
  run: |
    set -euo pipefail
    wheels=(dist/voco-*.whl)
    test "${#wheels[@]}" -eq 1
    uv venv .wheel-venv
    uv pip install --python .wheel-venv "${wheels[0]}"
```

For extras:

```yaml
- name: Install built wheel with production extras
  shell: bash
  run: |
    set -euo pipefail
    wheels=(dist/voco-*.whl)
    test "${#wheels[@]}" -eq 1
    uv pip install --python .release-venv/bin/python \
      "${wheels[0]}[mcp,stt,ptt,wake,floor]"
```

Preserve Windows compatibility in the matrix. If Bash array syntax is not
available on every runner, use a short checked Python helper to print the
single wheel path, or split the install step per OS. Do not replace the hard
code with an unchecked glob that could install stale wheels.

## Verification between phases

The user explicitly requested a double-check between phases. After each phase:

1. Run the smallest relevant test set under `set -e`.
2. Run Ruff on touched files.
3. Run `uv run mypy` for Python changes or TypeScript for browser changes.
4. Run `git diff --check`.
5. Inspect `git diff --stat` and the full touched-file diff.
6. Confirm no unrelated user changes were overwritten.
7. Make a focused commit only after the phase is green.

Do not chain independent checks in a way that lets the last successful command
mask an earlier failure. Use `set -e` or `&&`.

## Final release gate

Run from the repository root:

```bash
set -e
uv lock --check
uv sync --extra dev --extra mcp
uv run ruff check src clients tests tests_release scripts
uv run ruff format --check src clients tests tests_release scripts
uv run mypy
uv run pytest tests/ -q
uv run python scripts/gen_protocol.py
git diff --exit-code PROTOCOL.md
uv run python scripts/gen_status.py --check
npx -y -p typescript@5.6.3 tsc -p src/voco/server/static/tsconfig.json
npm run test:unit
npm ci
npm run test:e2e
```

Then perform fresh installed-wheel validation in temporary directories, not by
importing from the source checkout:

- Python 3.12 wheel with MCP smoke test.
- Python 3.11 wheel with `mcp,stt,ptt,wake,floor` and both release smoke tests.
- `voco --help` from the installed wheel.

The prior checkpoint passed 491 Python tests, four JavaScript tests, one real
Chromium Playwright E2E test, all lint/type/drift gates, and both wheel profiles.
The new final count should be higher because this handoff adds tests.

Also rerun dependency audits:

```bash
npm audit --audit-level=moderate
```

For Python, audit the locked full-extras graph under Python 3.11; that profile
supports `tflite-runtime` used by the wake extra. The previous audit reported
no known Python or npm vulnerabilities.

## Commit and tag discipline

Keep the existing rollback tag intact. Suggested sequence:

1. Commit the verified phase 1 speaker fix.
2. Commit bounded input/screen state and HTTP limits.
3. Commit browser/lifecycle/CI hardening.
4. Run the full release gate from a clean worktree.
5. Commit only generated status/protocol changes if the generators legitimately
   require them.
6. Create a new annotated checkpoint tag only after every gate passes, for
   example `checkpoint/post-audit-production-ready-20260713`.
7. Verify:

   ```bash
   git status --short
   git rev-parse HEAD
   git describe --tags --exact-match HEAD
   ```

Do not call the result production-ready if the worktree is dirty, a gate was
skipped, or the tag does not resolve exactly to the verified commit.

## Hardware validation still required

No remote/static pass can certify physical microphone unplug/replug behavior,
speaker device changes, acoustic echo cancellation quality, or measured
speech-to-dispatch latency. After software gates pass, run a short target-machine
hardware checklist:

- ordinary push-to-talk or VAD command to agent;
- agent speech playback and user barge-in;
- output device removal during playback produces visible `daemon.error`;
- input device removal/recovery produces visible state/error;
- browser page, diff, finding, ask, and MCP round trip;
- daemon restart preserves review state but does not route to ghost sessions;
- measure representative end-to-end latency rather than claiming a target from
  configuration alone.

## Final acceptance decision

The local-first review/MCP/workbench core can be called production-ready only
when:

- the P1 playback regression and full voice tests pass;
- queued inputs and screens are explicitly bounded with atomic failure;
- valid 1–8 MiB review payloads reach their domain validators;
- slow mutations cannot be presented as safely retryable failures;
- terminal cleanup and wheel CI fixes pass;
- the complete source/browser/wheel gates pass from a clean tagged commit; and
- the remaining shared-host auth and hardware limitations are documented as
  deployment boundaries, not silently implied capabilities.
