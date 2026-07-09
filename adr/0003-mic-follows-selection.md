# ADR-0003 — the mic follows selection (with an explicit lock)

Date: 2026-07-09 · Status: accepted (supersedes the explicit-mic rule
in ADR-0001 / DESIGN-DECK rev 5 "view and mic are separate state")

## Decision

Clicking an AGENT — tree row or deck card — views their work AND
routes the mic to them: selection is routing, like eye contact. A 🔒
lock (master card; mirrored in the command bar's `route → X 🔒`) pins
the mic for split attention: while locked, agent clicks are view-only
(steel), the pinned holder keeps amber, and only explicit movers
re-route — the card patch button (which appears only while locked),
⌘K "mic → X", or a spoken switch phrase. Mic moves initiated by voice
or another client pull the selection along unless locked. The mic
follows PEOPLE, never places: work-row and page browsing derive a view
focus and never touch routing.

## Why the old rule lost

The explicit-mic invariant taxed the COMMON action (switching who you
talk to — constant in multi-agent driving) to protect the RARE one
(reading agent A while dictating to B). Optimizing that backwards made
every conversation switch a special gesture, and grew a two-color
state language the user had to carry everywhere. The lock keeps the
rare case one deliberate act, and the dual steel/amber marking now
appears exactly and only in the state it describes. Two guards make
accidental re-routes near-impossible in practice: the route indicator
always names the target, and under ptt_only speech only happens while
the key is held.

## Consequences

- The deck's per-card patch is an override affordance shown only while
  locked; the holder always wears the MIC badge.
- DESIGN-DECK.md's scoping table (rev 5) is historical on this point.
- The lock is deliberately ephemeral (defaults unlocked per load);
  persist it only if real use asks for it.
