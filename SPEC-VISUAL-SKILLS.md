# VocoDeck Visual Skills — sharable interactive artifact surfaces

Status: **v0.1 draft** (2026-07-10) — captain-initiated concept, first
written form; not yet adversarially reviewed or grilled. Companion to
[SPEC.md](SPEC.md) (foundation) and
[SPEC-WORKBENCH.md](SPEC-WORKBENCH.md) (pages/review surfaces, whose
html pages and ask/answer plumbing this spec builds directly on).

## §1 The primitive

A skill today is a prompt package: it changes how the agent THINKS. The
deck already renders agent-published HTML pages: that changes what the
user SEES. A **visual skill** adds the missing third leg: it defines how
the user ACTS BACK. The unlock is not the HTML — it is a small `voco`
bridge object injected into the artifact so the page can send
**structured user input** to the agent and subscribe to a narrow slice
of the live event bus. The moment an artifact can call
`voco.input({...})` and react to `stt.partial`, it stops being a report
and becomes an **instrument**: a disposable, task-specific GUI the agent
conjured on demand.

Structured input is the point. Today the user's only channels back to an
agent are voice and free text. A bridge artifact returns TYPED answers —
a tapped card, a dragged weight, a selected row — as clean JSON the
agent consumes directly instead of prose it must parse.

## §2 Why voice makes this more valuable, not less

Voice is high-bandwidth IN, painfully low-bandwidth OUT (listening is
slow). The product brief already encodes the answer: speak one line,
show the substance. Visual skills productize that asymmetry — every
skill author designs the "show" half, and the loop closes without the
user touching a terminal. Nobody else has this combination: Claude
artifacts and ChatGPT canvas render HTML with no control plane behind
them; MCP has tools but no user-facing surface. VocoDeck has both, plus
the mic.

## §3 What a visual skill is (package shape)

```
my-skill/
  SKILL.md          # the prompt half: when/how the agent uses it
  artifact/         # the surface half: html/js templates using the bridge
    panel.html
  manifest.toml     # name, version, permissions, event subscriptions
```

- `SKILL.md` is a normal agent skill (Claude/Codex compatible); its
  instructions tell the agent to publish `artifact/panel.html` via the
  existing `page.publish` (type `html`) and how to interpret the
  `voco.input` payloads that come back.
- The artifact is plain HTML/JS. It talks ONLY to the injected bridge.
- The manifest declares the permission set (§5) and the event
  subscriptions the artifact may request. No manifest = local-trusted
  tier only (§5).

Distribution v1 is a directory: GitHub repos, dotfile-style. A gallery
("the vocodeck platform") is explicitly LATER (§8); nothing in v1 may
assume a central registry exists.

## §4 The bridge API (v1 — brutally small, it is a public contract)

Injected into html pages by the deck's htmlview host as `window.voco`
(iframe side of a postMessage broker; the artifact never sees the WS or
the daemon directly):

- `voco.input(payload: object) -> Promise<void>` — structured user
  action, routed to the agent. v1 transport: answers the page's pending
  ask when one exists (the B2-17 ask/answer plumbing IS this channel);
  otherwise delivered as a `say_as_user` envelope
  `{"artifact_input": {page_id, payload}}` so the agent receives it on
  the same path as speech, clearly marked as structured.
- `voco.subscribe(type: string, cb) -> unsubscribe` — live events,
  allowlisted per manifest from: `mic.state`, `turn.state`,
  `stt.partial`, `stt.final`, `speech.started`, `speech.sentence`,
  `mic.level`. Nothing else in v1 (no workspace/finding/session events —
  those carry other agents' and repos' data).
- `voco.state() -> Promise<object>` — a snapshot scoped to the SAME
  allowlist (mic state, turn state). Not the full store.

Everything else waits. Version the bridge (`voco.v = 1`); artifacts must
feature-test, the host must refuse unknown requested versions loudly.

## §5 Security model (design the broker before the gallery)

A marketplace means UNTRUSTED HTML adjacent to a control plane that can
spawn sessions and inject text into agents. Non-negotiables:

1. Artifacts render in a sandboxed iframe (`sandbox="allow-scripts"`,
   no `allow-same-origin`), served from a null/segregated origin.
2. All capability flows through the postMessage broker in the host
   page; the iframe has no network expectation (v1 may leave iframe
   network unrestricted-by-CSP only for local-trusted skills; manifest
   tier declares `network = false` and the host enforces via CSP).
3. Per-skill permission manifest, extension-style: e.g.
   `permissions = ["input", "subscribe:stt.partial", "subscribe:turn.state"]`.
   First use of a manifested skill prompts the user once with the
   plain-language list; the grant is pinned to the artifact content
   hash — a changed artifact re-prompts.
4. Trust tiers: **local-trusted** (user installed it on disk; full v1
   bridge, no prompt) → **manifested** (third-party; prompt + hash pin)
   → **gallery** (later; adds signing/review). v1 ships ONLY
   local-trusted; the tiering exists so the broker API never has to
   change shape when the gallery arrives.
5. `voco.input` payloads are size-capped and JSON-only; the agent-side
   envelope always names the page_id and skill so an agent can refuse
   input from surfaces it did not publish.
6. This workstream and BUILD-PROD **P8 (auth posture)** are siblings:
   the bridge must never widen what an unauthenticated loopback client
   could already do.

## §6 Runtime lifecycle

Publish: the agent publishes the artifact as a normal html page
(rev-bumped like any page; annotations continue to work). Mount: the
htmlview host wraps it in the sandboxed iframe + broker and injects the
bridge. Input: `voco.input` resolves when the daemon acks. Events:
subscriptions auto-detach when the page closes or a new rev replaces it
(the new rev re-subscribes itself). Disconnect: the broker surfaces
`voco.subscribe("conn", ...)`? — NO (v1): on WS loss the host simply
freezes the artifact and shows the deck's own offline treatment; the
artifact must not need conn logic.

## §7 Example skills (the blanks, filled)

- **Ask deck** (the MVP demo, §8): open asks render as tappable cards;
  a tap answers the ask. Dogfoods B2-17 end to end.
- **Approval deck**: PR-review findings as swipe/tap cards →
  approve/reject/defer per finding in seconds, agent narrates only
  what needs a human sentence.
- **Decision matrix**: options × criteria grid, user pokes weights,
  agent receives `{choice, weights}`.
- **Voice-reactive forms**: config wizard highlighting/filling fields
  from `stt.partial` live ("set port to eighty-eighty").
- **Teleprompter/coaching surfaces**: scroll and score driven by
  `speech.sentence` / `stt` timing.
- **Ritual consoles**: standup collector, retro board, incident
  commander, release checklist — shared like dotfiles; the skill IS
  the app.
- **Hands-busy panels**: giant-button cooking/lab/workshop controls +
  voice.

## §8 Milestones

- **VS0 — broker + bridge v1 + ask deck** (local-trusted only): inject
  the postMessage broker into the htmlview host, implement the three
  verbs with the ask-answer transport, allowlist `turn.state` +
  `stt.partial` + `mic.level`, ship the ask-deck skill in-repo as the
  reference artifact. Depends on B2-17 (asks in dock) landing first.
- **VS1 — manifest tier**: manifest.toml, permission prompt, content
  hash pinning, CSP enforcement for `network = false`.
- **VS2 — sharing**: installer verb (`voco skill add <path|git url>`),
  docs, 3–5 polished reference skills. Gallery/registry remains out of
  scope until VS2 proves demand.

## §9 Non-goals (v1)

No central registry/marketplace; no artifact access to workspaces,
findings, sessions, or other agents; no artifact-initiated speech
(`speak` stays agent-only — a surface that talks is an agent
impersonation vector); no cross-artifact messaging; no persistence API
(artifacts are disposable — durable state belongs to the agent/skill).

## §10 Open questions (for the grill)

1. Does `voco.input` need a reply channel (agent → artifact) beyond
   re-publishing a new rev? (Lean: no — rev republish IS the reply; it
   keeps artifacts stateless.)
2. Should `stt.partial` be gated behind an extra "listening indicator"
   requirement so a surface can't silently transcribe? (Lean: yes at
   the manifested tier: subscribing forces a visible host-drawn badge.)
3. Where does the bridge host live as the deck evolves (this spec must
   survive a possible framework/Tauri port — §Productization, separate
   discussion)?
4. Payload size caps and rate limits for `voco.input` (flood = agent
   spam).

## §11 Decision log

1. (2026-07-10, captain + firstmate) Concept adopted; spec drafted.
   Bridge kept to three verbs; ask/answer plumbing chosen as the v1
   input transport; local-trusted tier first; broker-before-gallery
   ordering is a hard rule.
