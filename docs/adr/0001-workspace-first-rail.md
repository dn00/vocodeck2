# Workspace-first rail (reverses DESIGN-DECK rev 3's demotion)

The rail's primary node is the workspace (unit of work: worktree +
issue/PR link + agents + pages), grouped by repo — not the agent.
Decided 2026-07-07 after the gh issue/PR linkage requirement surfaced;
reverses rev 3 (2026-07-06), which demoted workspaces to data-only and
grouped agents directly under repos.

Why: agents are ephemeral by design (session churn, predecessor sweep)
while everything the product persists — findings ledgers, pages,
manifests — is workspace-keyed; a rail anchored on the churning axis
kept failing (grey-corpse sessions, agentless review had no home, the
issue/PR link has no honest place on an agent node). Scenario analysis:
work-first wins on agent restarts, parallel worktrees, and agentless
review; ties on the single-agent common case (compact rows keep it one
click); loses slightly on repo-less sessionspaces (rendered as bare
agent rows).

Consequences: view selection and mic split into two pieces of state.
Invariants that keep that honest: the mic moves ONLY on explicit agent
click or spoken switch phrase (work rows never steal it), and the
presence strip always names the mic holder. "Workspace" never appears
as a UI word — rows are labeled by branch + issue/PR chip (SPEC
decision 20 untouched: presentation, not data identity).
