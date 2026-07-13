# Vocodeck

A voice control plane for coding agents: you talk to agents working in
terminals; agents talk back; their work (diffs, docs, terminals) is
reviewed in a browser workbench. Work is durable; workers are ephemeral.

## Language

**Workspace**:
The durable unit of work — one checkout (often a git worktree) holding
its pages, findings, asks, and any agents working in it. May be linked
to a GitHub issue or PR. Internal term only: the UI never shows the word.
_Avoid_: project, container, group

**Work row**:
A workspace as rendered in the rail, labeled by its branch plus an
issue/PR chip when linked. Selecting one changes what you view, never
who you talk to.
_Avoid_: workspace (in UI copy), folder

**Agent**:
An ephemeral worker — one attached harness session — working inside a
workspace. Agents restart and churn; their workspace persists.
_Avoid_: session (in UI copy), bot

**Mic (voice target)**:
The single agent that receives spoken input. Moves only by explicit
agent selection or a spoken switch phrase — never implicitly.
_Avoid_: active agent, focus

**View selection**:
The workspace or agent whose artifacts fill the center and dock.
Independent of the mic: you can look at one thing while talking to
another agent.
_Avoid_: selection (unqualified)

**Sessionspace**:
The degenerate workspace of a repo-less agent (cwd, no checkout).
Renders as a bare agent row.

**Repo group**:
The rail grouping of workspaces that share one git common dir (a repo
and its worktrees).
_Avoid_: project

**Link**:
A workspace's association to a GitHub issue or PR, shown as a chip on
its work row. Auto-discovered via gh when available (silently absent
otherwise) or set by hand; git — not gh — supplies all repo facts.

**Page**:
One reviewable artifact in a workspace: a diff, a doc, a screen, or a
terminal.

**Finding** (UI: **annotation**):
One review remark on a page, living in its workspace's ledger; wakes an
agent and persists until addressed or withdrawn.
_Avoid_: comment, issue

**Review**:
The activity of examining a page and making annotations. Needs no
agent.

## Example dialogue

> **Dev:** Freya restarted overnight — did we lose the review?
> **Expert:** No. The *workspace* for `fix-auth` still has the diff page
> and both open *findings*; Freya's new session re-attached to it. The
> *work row* never moved.
> **Dev:** I clicked the `#123` row to check the PR — who am I talking
> to now?
> **Expert:** Still Freya. Clicking a work row changes your *view
> selection*; the *mic* only moves when you click an agent or say a
> switch phrase.
