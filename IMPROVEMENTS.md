# vocodeck2 — Improvement Notes

1. **Doc annotation toggle** — add a button to turn annotations on/off for doc pages (same pattern as diff annotations but switchable)
2. **Mic meter in PTT mode** — when in push-to-talk mode and not holding the button, the mic meter should show muted/inactive state, not live signal
3. **File tree + viewer side-by-side** — file tree panel should sit alongside the file viewer, not replace it (currently it's either/or)
4. **Queue drain bug** — queued inputs show on the agent card but don't seem to drain to the listener script; likely same root cause as #8 (agent state)
5. **Duplex mic animation** — in duplex/full mode, the mic meter animation should not run while the agent is speaking (same idea as #2 but for duplex)
6. **Annotation submit button** — no visible submit/send button when adding annotations; need a clear way to submit findings to the agent
7. **Mute TTS toggle** — ability to turn off speech/TTS responses from the deck UI without disabling the mic
8. **Agent working state** — deck always shows "ready"; should detect when the agent is actively working and display "working" state instead
9. **File path breadcrumb** — for docs, HTML, and path-backed pages, show the file path in a status bar below the tabs in the center panel
10. **PTT key visibility + settings** — show the current push-to-talk hotkey in the deck (currently F9); allow configuring it from a settings UI in the workbench
11. **UX polish (holistic)** — the UI is not production-ready; settings page is just raw input fields, needs proper controls, grouping, labels, and validation. Applies broadly across the workbench — needs real design pass for user-friendliness
12. **Framework migration** — consider moving from buildless vanilla .mjs to a proper framework (Svelte/React) inside Tauri for a native desktop app. Pros: native hotkeys, system tray, no browser dependency, agents trained on framework patterns. Cons: adds Rust + build step, loses zero-install browser access. Open question — needs decision.
13. **Agent card text overflow** — long agent responses (say_tail) stretch the card and clip text; only shows the last part, not the full message. Needs proper truncation with expand/scroll, and should show the beginning or full text
