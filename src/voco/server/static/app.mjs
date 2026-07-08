// @ts-check
/**
 * Workbench entry (SPEC-WORKBENCH §7; DESIGN-DECK rev 5, U2b) —
 * workspace-first (ADR-0001).
 *
 * The rail is the WORK: repo groups → work rows (branch + issue/PR
 * chip; agents and pages nested) → the durable nodes. Agents are
 * ephemeral workers attached to work. VIEW and MIC are separate state:
 * clicking a work row changes what you look at; ONLY clicking an agent
 * (or a spoken switch phrase) moves the mic — and the presence strip
 * always names the mic holder. "Workspace" is never a UI word. The
 * dock (annotations | transcript) follows the view and says so; the
 * status line carries ambient truth. Disconnected is a designed state.
 */

import { Store } from "./store.mjs";
import { connectBus } from "./bus.mjs";
import { renderMarkdown } from "./markdown.mjs";
import { renderDiff, diffStats, seedFolds } from "./diff.mjs";
import { renderFindings } from "./findings.mjs";
import { renderTranscript, flashEntry } from "./transcript.mjs";
import { renderPresence } from "./presence.mjs";
import { renderTerminal } from "./term.mjs";
import { openPicker, openRepo, openSpawn, openConnect, openSettings,
  confirmDanger } from "./modals.mjs";

const store = new Store();
const bus = connectBus(store);

const h = (tag, attrs = {}, ...kids) => {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") el.className = v;
    else if (k === "onclick") el.addEventListener("click", v);
    else if (k === "html") el.innerHTML = v;
    else if (v != null) el.setAttribute(k, v);
  }
  for (const kid of kids)
    if (kid) el.append(kid instanceof Node ? kid : document.createTextNode(kid));
  return el;
};

// ---- shell ------------------------------------------------------------------
const app = /** @type {HTMLElement} */ (document.getElementById("app"));
const presence = h("div", { class: "presence" });
const rail = h("div", { class: "rail" });
const gripRail = h("div", { class: "grip", role: "separator", tabindex: "0",
  "aria-label": "resize rail" });
const work = h("div", { class: "work" });
const gripDock = h("div", { class: "grip", role: "separator", tabindex: "0",
  "aria-label": "resize dock" });
const dock = h("div", { class: "dock" });
const statusline = h("div", { class: "statusline" });
const body = h("div", { class: "deck-body" }, rail, gripRail, work, gripDock, dock);
app.append(presence, body, statusline);

// ---- toasts (policy: errors persist w/ dismiss; successes fade) ---------------
function toast(msg, sticky = false) {
  const t = h("div", { class: "toast-msg" + (sticky ? " sticky" : "") }, msg);
  if (sticky)
    t.append(h("span", { class: "toast-x", title: "dismiss",
      onclick: () => t.remove() }, "✕"));
  document.body.append(t);
  if (!sticky) setTimeout(() => t.remove(), 4000);
}

/** Undo-over-confirm (design policy): success-style toast carrying the
 * one-click undo; fades like a success, because it is one. */
function toastUndo(msg, onUndo) {
  const t = h("div", { class: "toast-msg" }, msg + " ",
    h("button", { class: "toast-undo",
      onclick: async () => { t.remove(); await onUndo(); } }, "undo"));
  document.body.append(t);
  setTimeout(() => t.remove(), 6000);
}
const errMsg = (e) => (e instanceof Error ? e.message : String(e));

// ---- modal contexts -------------------------------------------------------------
const mctx = () => ({ command: (c, p) => bus.command(c, p), toast });

function openPickerFor(ws) {
  openPicker({ ...mctx(), ws,
    onOpened: (r, wsKey) => {
      const w = store.workspaces.get(wsKey);
      if (w) { selectWork(w); store.selectPage(r.page_id); }
      toast(`diff opened · r${r.rev}`);
    } });
}

// ---- content cache (page_id -> {rev, content}) ------------------------------
const contentCache = new Map();
async function fetchContent(pageId, rev) {
  const hit = contentCache.get(pageId);
  if (hit && hit.rev === rev) return hit.content;
  const resp = await fetch(`/v1/page/${pageId}`, {
    headers: { "x-voco-wb": (window.__VOCO__ || {}).wb || "" },
  });
  if (!resp.ok) throw new Error(`page ${pageId}: ${resp.status}`);
  const body = await resp.json();
  contentCache.set(pageId, { rev, content: body.content });
  return body.content;
}

// ---- identity helpers ---------------------------------------------------------
const stripSlash = (p) => String(p || "").replace(/\/+$/, "");

function sessionInWs(s, ws) {
  if (s.host != null && s.root != null)
    return s.host === ws.host && stripSlash(s.root) === stripSlash(ws.root);
  return ws.pages.some((p) => p.call_name === s.name);
}

function wsOf(s) {
  return [...store.workspaces.values()].find((w) => sessionInWs(s, w)) || null;
}

function selectedAgent() {
  return store.selectedAgent ? store.sessions.get(store.selectedAgent) : null;
}

// herdr borrow: blocked is loud — attention-first ordering.
const STATE_ORDER = { blocked: 0, working: 1, listening: 2, idle: 3, stale: 4, gone: 5 };
const stateOf = (s) => s.display_state || s.state || "idle";

function dot(s) {
  const state = stateOf(s);
  return h("span", { class: "dot " + state, title: state });
}

function agentsIn(ws) {
  return [...store.sessions.values()]
    .filter((s) => sessionInWs(s, ws))
    .sort((a, b) =>
      (STATE_ORDER[stateOf(a)] ?? 9) - (STATE_ORDER[stateOf(b)] ?? 9)
      || a.name.localeCompare(b.name));
}

const workLabel = (ws) => String(ws.branch || ws.name);

// ---- selection: VIEW (work) vs MIC (agent) — ADR-0001 --------------------------
// gh is optional by decision: detection is lazy, cached, and silent.
// The tried-set keys on branch too — a branch switch re-detects (the
// daemon drops gh-sourced links on branch change for the same reason).
const detectTried = new Set();
function lazyDetectLinks(ws) {
  if (!ws || ws.kind !== "workspace") return;
  const tryKey = ws.key + "@" + (ws.branch || "");
  if (detectTried.has(tryKey)) return;
  const l = ws.links || {};
  if (l.pr || l.issue) return;
  detectTried.add(tryKey);
  bus.command("workspace.link", { workspace: ws.key, detect: true })
    .catch(() => {});
}

/** View-only: what you look at changes; the mic does NOT move. */
function selectWork(ws) {
  const agents = agentsIn(ws);
  // A lone worker is the row's obvious conversation — focus its
  // transcript (focus is view state; the mic still doesn't move).
  store.selectedAgent = agents.length === 1 ? agents[0].session_id : null;
  store.selectWorkspace(ws.key);
  if (agents.length === 1) loadTranscript(agents[0]);
  lazyDetectLinks(ws);
}

/** The ONLY mic-mover in the deck (besides spoken switch phrases). */
async function selectAgent(s) {
  store.selectedAgent = s.session_id;
  const ws = wsOf(s);
  store.selectedWorkspace = ws ? ws.key : null;
  const open = ws ? ws.pages.filter((p) => !p.closed) : [];
  const own = open.find((p) => p.call_name === s.name && p.type === "screen")
    || open.find((p) => p.call_name === s.name);
  store.selectedPage = own ? own.page_id : null;
  store._notify("selection");
  loadTranscript(s);
  lazyDetectLinks(ws);
  try { await bus.command("switch_session", { name: s.name }); }
  catch (e) { toast("activate failed: " + errMsg(e), true); }
  if (ws && (!Array.isArray(s.capabilities) || s.capabilities.includes("review"))) {
    try { await bus.command("review.primary", { workspace: ws.key, agent: s.name }); }
    catch (e) { /* agent may lack review capability; the dock says so */ }
  }
}

async function detachAgent(s) {
  // Undo-over-confirm (design policy): detach never touches the process
  // and the agent re-registers on its next call — no dialog earned.
  try {
    await bus.command("session.detach", { name: s.name });
    toast(`forgot ${s.name} — it reappears on its next call`);
  } catch (e) { toast("detach failed: " + errMsg(e), true); }
}

// ---- rail: repo groups → work rows → agents + pages ----------------------------
const PAGE_ICON = { screen: "▦", diff: "±", doc: "¶", terminal: "❯" };

function groupKey(ws) { return ws.common_dir || ws.key; }

function renderRail() {
  const keep = rail.scrollTop; // rebuilds must not move the reader
  rail.replaceChildren();
  // Repo groups hold WORK ROWS (kind=workspace only); sessionspace
  // agents render as bare agent rows under "elsewhere" (ADR-0001).
  /** @type {Map<string, {name:string, works:any[]}>} */
  const groups = new Map();
  for (const ws of store.workspaces.values()) {
    if (ws.kind !== "workspace") continue;
    const key = groupKey(ws);
    if (!groups.has(key)) groups.set(key, { name: ws.repo || ws.name, works: [] });
    const g = /** @type {{name:string, works:any[]}} */ (groups.get(key));
    g.works.push(ws);
    if (ws.repo) g.name = ws.repo;
  }
  const placed = new Set();
  for (const [, g] of groups) {
    const groupWs = () =>
      g.works.find((w) => w.key === store.selectedWorkspace) || g.works[0];
    rail.append(h("div", { class: "repo-group" },
      h("span", { class: "rname" }, g.name),
      h("button", { class: "review-btn", title: "review a diff here — no agent needed",
        onclick: () => openPickerFor(groupWs()) }, "review")));
    const rows = g.works.map((ws) => ({ ws, agents: agentsIn(ws) }))
      .sort((a, b) => rowOrder(a) - rowOrder(b)
        || workLabel(a.ws).localeCompare(workLabel(b.ws)));
    for (const r of rows) {
      for (const s of r.agents) placed.add(s.session_id);
      rail.append(workRow(r.ws, r.agents));
    }
    rail.append(h("div", { class: "rail-action",
      onclick: () => openSpawn({ ...mctx(), rootHint: groupWs().root }) },
      "＋ agent in a new worktree…"));
  }
  // Agents outside any checkout (sessionspaces, pre-identity strays).
  const stray = [...store.sessions.values()].filter((s) => !placed.has(s.session_id));
  if (stray.length) {
    rail.append(h("div", { class: "repo-group" },
      h("span", { class: "rname", title:
        "agents running outside any git checkout" }, "not in a repo")));
    for (const s of stray) rail.append(agentRow(s, true));
  }
  if (!store.sessions.size && !store.workspaces.size)
    rail.append(h("div", { class: "empty-note" },
      "no agents — run voice_init (MCP) or `voco listen` in a repo"));
  rail.append(h("div", { class: "rail-action rail-foot",
    onclick: () => openConnect(mctx()) }, "connect →"));
  rail.scrollTop = keep;
}

// Blocked work first; parked (agentless) work below live work, above gone.
const rowOrder = (r) =>
  r.agents.length ? (STATE_ORDER[stateOf(r.agents[0])] ?? 9) : 4.5;

function workRow(ws, agents) {
  const sel = store.selectedWorkspace === ws.key;
  const single = agents.length === 1 ? agents[0] : null;
  const row = h("div", {
    class: "work-row" + (sel ? " sel" : "") + (agents.length ? "" : " parked"),
    onclick: () => selectWork(ws) },
    h("div", { class: "wr-top" },
      agents.length ? dot(agents[0]) : h("span", { class: "dot none", title: "no agent" }),
      h("span", { class: "wr-label" }, workLabel(ws)),
      ...linkChips(ws),
      flaggedChip(ws)),
    h("div", { class: "wr-sub" },
      single ? inlineAgent(single)
        : h("span", { class: "wr-note" }, agents.length
          // state word rides the note: state is never color-only (a11y)
          ? `${agents.length} agents · ${stateOf(agents[0])}`
          : "no agent")));
  if (!sel) return row;
  const box = h("div", {}, row);
  if (!single) for (const s of agents) box.append(agentRow(s));
  box.append(pagesTree(ws));
  return box;
}

/** The compact single-worker cluster ON the work row: clicking the
 * agent moves the mic; clicking anywhere else on the row is view-only. */
function inlineAgent(s) {
  return h("span", { class: "wr-agent",
    title: `talk to ${s.name} (moves the mic)`,
    onclick: (e) => { e.stopPropagation(); selectAgent(s); } },
    h("span", { class: "ar-name" }, s.name),
    store.activeSession === s.session_id
      ? h("span", { class: "bolt", title: "holds the mic" }, "⚡") : "",
    store.speaking && store.speaking.who === s.name ? speakingEq() : "",
    h("span", { class: "ar-state " + stateOf(s) }, stateOf(s)),
    s.queued ? h("span", { class: "chip hot" }, s.queued + " queued") : "",
    h("span", { class: "rail-x", title: "forget this session",
      onclick: (e) => { e.stopPropagation(); detachAgent(s); } }, "✕"));
}

/** Nested (inside a selected multi-agent row) or bare (sessionspace). */
function agentRow(s, bare = false) {
  const sel = store.selectedAgent === s.session_id;
  const row = h("div", {
    class: "agent-row" + (bare ? "" : " nested") + (sel ? " sel" : ""),
    onclick: () => selectAgent(s) },
    h("div", { class: "ar-top" },
      dot(s),
      h("span", { class: "ar-name" }, s.name),
      store.activeSession === s.session_id
        ? h("span", { class: "bolt", title: "holds the mic" }, "⚡") : "",
      store.speaking && store.speaking.who === s.name ? speakingEq() : "",
      h("span", { class: "ar-state " + stateOf(s) }, stateOf(s)),
      s.queued ? h("span", { class: "chip hot" }, s.queued + " queued") : "",
      h("span", { class: "rail-x", title: "forget this session",
        onclick: (e) => { e.stopPropagation(); detachAgent(s); } }, "✕")));
  if (bare && sel) {
    const ws = wsOf(s);
    if (ws) return h("div", {}, row, pagesTree(ws));
  }
  return row;
}

function speakingEq() {
  const eq = h("span", { class: "eq", title: "speaking aloud" });
  for (let i = 0; i < 3; i++) eq.append(h("i"));
  return eq;
}

function flaggedChip(ws) {
  if (!ws) return "";
  // Unvisited rows fall back to the snapshot's counts — parked work's
  // open annotations must be visible without selecting it first.
  const open = store.findings.has(ws.key)
    ? store.findingsFor(ws.key).filter((f) => f.status === "open").length
      + store.asksFor(ws.key).filter((a) => a.answer == null).length
    : ((ws.finding_counts && ws.finding_counts.open) || 0)
      + (ws.open_asks || 0);
  return open ? h("span", { class: "chip amber" }, open + " flagged") : "";
}

function linkChip(kind, link) {
  const chip = h("span", {
    class: "link-chip" + (link.url ? " go" : ""),
    title: link.title || `${kind} ${link.number}` },
    (kind === "pr" ? "pr#" : "#") + link.number);
  if (link.url)
    chip.addEventListener("click", (e) => {
      e.stopPropagation();
      window.open(link.url, "_blank", "noopener");
    });
  return chip;
}

function linkChips(ws) {
  const l = ws.links || {};
  return [l.issue ? linkChip("issue", l.issue) : null,
    l.pr ? linkChip("pr", l.pr) : null];
}

function pagesTree(ws) {
  const tree = h("div", { class: "pages" });
  tree.append(h("div", {
    class: "page-row" + (store.selectedPage == null ? " sel" : ""),
    onclick: (e) => { e.stopPropagation(); store.selectPage(null); } },
    h("span", { class: "picon" }, "◈"), h("span", {}, "overview")));
  const pages = ws
    ? ws.pages.filter((p) => !p.closed)
      .sort((a, b) => (a.pinned ? 0 : 1) - (b.pinned ? 0 : 1)
        || a.page_id.localeCompare(b.page_id))
    : [];
  for (const p of pages) {
    const row = h("div", {
      class: "page-row" + (p.page_id === store.selectedPage ? " sel" : ""),
      onclick: (e) => { e.stopPropagation(); store.selectPage(p.page_id); } },
      h("span", { class: "picon" }, PAGE_ICON[p.type] || "·"),
      h("span", { class: "page-title" }, p.title),
      p.rev > 1 ? h("span", { class: "rev",
        title: `revision ${p.rev} — republished ${p.rev - 1}×` },
        "r" + p.rev) : "");
    if (!p.pinned)
      row.append(h("span", { class: "rail-x", title: "close page",
        onclick: (e) => { e.stopPropagation(); closePage(p); } }, "✕"));
    tree.append(row);
    // The selected diff's file sub-tree (rev 4.1 rail): stats + finding
    // dot per file; click = open that fold and jump to it.
    if (p.type === "diff" && p.page_id === store.selectedPage) {
      const sub = diffSubTree(ws, p);
      if (sub) tree.append(sub);
    }
  }
  return tree;
}

function diffSubTree(ws, p) {
  const cached = contentCache.get(p.page_id);
  if (!cached || cached.rev !== p.rev) return null;
  const st = diffStats(cached.content.files || []);
  if (!st.files) return null;
  const flagged = new Set(store.findingsFor(ws.key)
    .filter((f) => f.status === "open" && f.page_id === p.page_id)
    .map((f) => (f.anchor || {}).file));
  const box = h("div", { class: "dfiles" });
  for (const [path, s] of st.perFile) {
    box.append(h("div", { class: "dfile-row",
      onclick: (e) => {
        e.stopPropagation();
        pendingReveal = { pageId: p.page_id, path };
        store.selectPage(p.page_id);
      } },
      flagged.has(path) ? h("span", { class: "fdot", title: "open annotation" }) : "",
      h("span", {}, path.split("/").pop()),
      h("span", { class: "dfm" },
        s.add ? h("span", { class: "add" }, "+" + s.add) : "",
        s.add && s.del ? " " : "",
        s.del ? h("span", { class: "del" }, "−" + s.del) : "")));
  }
  return box;
}

// Fold state per diff page — survives re-renders so the reader keeps
// their place; reseeded when the page rev moves (new diff, new folds).
const foldCache = new Map();
/** @type {?{pageId:string, path:string}} */
let pendingReveal = null;

// ---- work: crumb header + view -------------------------------------------------
// U2R render discipline (reference architecture): the center rebuilds
// ONLY when what it shows actually changed — a fingerprint gates it, so
// session pings / speech events / unrelated findings are no-ops here.
// Scroll is remembered PER PAGE and restored after every rebuild; async
// renders carry a token so a stale fetch can never paint over a newer one.
let lastWorkKey = "";
const scrollMemo = new Map(); // pageKey -> scrollTop

const pageKey = () =>
  (store.selectedPage || "overview") + ":" + (store.selectedWorkspace || "");

function workFingerprint() {
  const ws = store.selectedWs();
  const agent = selectedAgent();
  const pages = ws ? ws.pages.filter((p) => !p.closed) : [];
  const page = pages.find((p) => p.page_id === store.selectedPage);
  const parts = [store.selectedWorkspace, store.selectedPage,
    store.connected ? 1 : 0];
  if (page) {
    parts.push(page.rev, page.title);
    // findings shape the diff's marks/chips — only THIS page's matter
    if (page.type === "diff")
      parts.push(store.findingsFor(ws ? ws.key : "")
        .filter((f) => f.page_id === page.page_id)
        .map((f) => f.finding_id + f.status).join(","));
    if (page.type === "screen" && agent)
      parts.push((agent.screen_markdown || "").length);
  } else if (agent) {
    parts.push(stateOf(agent), agent.queued || 0,
      (agent.screen_markdown || "").length,
      store.activeSession === agent.session_id ? 1 : 0);
  } else if (ws) {
    parts.push(agentsIn(ws).map((a) => a.name + stateOf(a)).join(","),
      JSON.stringify(ws.links || {}));
  }
  return parts.join("|");
}

function renderWork(force = false) {
  const key = workFingerprint();
  if (!force && key === lastWorkKey && !pendingReveal) return;
  lastWorkKey = key;
  const oldView = work.querySelector(".view");
  if (oldView) scrollMemo.set(pageKey(), oldView.scrollTop);
  work.replaceChildren();
  const agent = selectedAgent();
  const ws = store.selectedWs();
  if (!agent && !ws) {
    work.append(h("div", { class: "view" }, welcomeView()));
    return;
  }
  const pages = ws ? ws.pages.filter((p) => !p.closed) : [];
  const page = pages.find((p) => p.page_id === store.selectedPage);
  const crumb = h("div", { class: "crumb" });
  crumb.append(
    h("span", { class: "crumb-who" }, ws ? workLabel(ws) : (agent ? agent.name : "")),
    h("span", { class: "sep" }, " / "),
    h("span", {}, page ? page.title : "overview"));
  if (page && page.rev > 1)
    crumb.append(h("span", { class: "sep" }, " · "),
      h("span", { class: "micro rev",
        title: `republished ${page.rev - 1}× — annotations from older` +
          " revisions are marked stale, never dropped" },
        "rev " + page.rev));
  const srnote = h("span", { class: "sr-note" });
  const actions = h("div", { class: "work-actions" });
  work.append(h("div", { class: "work-head" }, crumb, srnote, actions));
  const view = h("div", { class: "view" });
  view.addEventListener("scroll",
    () => scrollMemo.set(pageKey(), view.scrollTop), { passive: true });
  work.append(view);
  if (page) { renderPage(view, page, srnote, actions); return; }
  if (agent) { renderAgentCard(view, agent, ws); return; }
  if (ws) { renderWorkCard(view, ws); return; }
  view.classList.add("empty");
  view.append(h("div", { class: "empty-note" }, "no pages here yet"));
}

/** Scroll WITHIN the center view only — scrollIntoView walks every
 * scrollable ancestor and was yanking the whole deck around. */
function scrollViewTo(el, block = "start") {
  const view = work.querySelector(".view");
  if (!view || !el) return;
  const vr = view.getBoundingClientRect();
  const er = el.getBoundingClientRect();
  const offset = block === "center" ? (vr.height / 2 - er.height / 2) : 8;
  view.scrollTop += er.top - vr.top - offset;
}

function renderAgentCard(view, s, ws) {
  const card = h("div", { class: "agent-card" });
  card.append(h("div", { class: "agent-card-head" },
    dot(s),
    h("span", { class: "agent-card-name" }, s.display_name || s.name),
    store.activeSession === s.session_id
      ? h("span", { class: "agent-card-active" }, "⚡ holds the mic") : ""));
  card.append(h("div", { class: "agent-card-meta" },
    kv("state", stateOf(s)),
    kv("queued", String(s.queued || 0)),
    kv("capabilities", (s.capabilities || []).join(" ") || "?")));
  if (s.screen_markdown && s.screen_markdown.trim()) {
    const scr = h("div", { class: "agent-card-screen" });
    renderMarkdown(scr, s.screen_markdown);
    card.append(scr);
  } else {
    card.append(h("div", { class: "empty-note" },
      "nothing on this agent's screen yet — its pages appear in the rail as it publishes"));
  }
  view.append(card);
}

/** Overview of agentless (or multi-agent) work: the durable facts. */
function renderWorkCard(view, ws) {
  const agents = agentsIn(ws);
  const open = store.findingsFor(ws.key)
    .filter((f) => f.status === "open").length;
  const card = h("div", { class: "agent-card" });
  card.append(h("div", { class: "agent-card-head" },
    h("span", { class: "agent-card-name" }, workLabel(ws)),
    ...linkChips(ws)));
  card.append(h("div", { class: "agent-card-meta" },
    kv("repo", ws.repo || ws.name),
    kv("root", ws.root),
    kv("agents", agents.length ? agents.map((a) => a.name).join(" ") : "none"),
    kv("open", String(open))));
  card.append(h("div", { class: "empty-note" }, agents.length
    ? "select a page in the rail — or click an agent to talk to it"
    : "review-only: no agent attached — diffs and annotations still work"));
  view.append(card);
}

function welcomeView() {
  const reviewSomewhere = () => {
    const anyWs = [...store.workspaces.values()].find((w) => w.kind === "workspace");
    if (anyWs) openPickerFor(anyWs);
    else openRepo({ ...mctx(), onOpened: (wsKey) => {
      const w = store.workspaces.get(wsKey);
      if (w) { selectWork(w); openPickerFor(w); }
    } });
  };
  return h("div", { class: "empty-card" },
    h("h4", {}, "Nothing on the deck yet"),
    h("p", {}, "Pick any of these — no setup order, no terminal required."),
    h("div", { class: "empty-actions" },
      h("button", { class: "btn-primary", onclick: () => openSpawn(mctx()) },
        "spawn an agent"),
      h("button", { class: "btn-ghost", onclick: reviewSomewhere },
        "review a diff"),
      h("button", { class: "btn-ghost", onclick: () => openRepo({ ...mctx(),
        onOpened: (wsKey) => {
          const w = store.workspaces.get(wsKey);
          if (w) selectWork(w);
        } }) }, "open a repo…")),
    h("div", { class: "empty-hints" },
      h("span", {}, "already running Claude Code or Codex somewhere?"),
      h("button", { onclick: () => openConnect(mctx()) }, "connect →")));
}

const kv = (k, v) => h("span", { class: "st-item" },
  h("span", { class: "k" }, k), h("span", {}, v));

// Async render token: only the LATEST renderPage may touch the view —
// a slow fetch finishing late must not paint over a newer render (this
// was the "glitchy markdown": racing renders interleaving).
let renderSeq = 0;

function restoreScroll(view) {
  const saved = scrollMemo.get(pageKey());
  if (saved != null && !pendingReveal) view.scrollTop = saved;
}

async function renderPage(view, page, srnote, actions) {
  const seq = ++renderSeq;
  const stale = () => seq !== renderSeq || !view.isConnected;
  if (page.type === "screen" || page.type === "doc") {
    view.textContent = "…";
    try {
      const c = await fetchContent(page.page_id, page.rev);
      if (stale()) return;
      view.replaceChildren();
      await renderMarkdown(view, c.markdown || "");
      if (!stale()) restoreScroll(view);
    } catch (e) { if (!stale()) view.textContent = "could not load: " + errMsg(e); }
    return;
  }
  if (page.type === "diff") {
    view.textContent = "…";
    try {
      const cold = (contentCache.get(page.page_id) || {}).rev !== page.rev;
      const c = await fetchContent(page.page_id, page.rev);
      if (stale()) return;
      // First load of this rev: the rail's file sub-tree can render now.
      if (cold) requestAnimationFrame(renderRail);
      const findings = store.findingsFor(store.selectedWorkspace || "")
        .filter((f) => f.page_id === page.page_id && f.status !== "withdrawn");
      let fc = foldCache.get(page.page_id);
      if (!fc || fc.rev !== page.rev) {
        fc = { rev: page.rev, fold: seedFolds(c, findings),
          seededEmpty: findings.length === 0 };
        foldCache.set(page.page_id, fc);
      } else if (fc.seededEmpty && findings.length) {
        // Findings lazy-load AFTER the first render (fresh snapshot):
        // union their folds in once — user folds are never removed
        // (xai U2c W3).
        for (const path of seedFolds(c, findings)) fc.fold.add(path);
        fc.seededEmpty = false;
      }
      // Consume the reveal only if it targets THIS page — a stale async
      // render must not steal a newer page's jump (xai U2c W1).
      let reveal = null;
      if (pendingReveal && pendingReveal.pageId === page.page_id) {
        reveal = pendingReveal.path;
        pendingReveal = null;
      }
      view.replaceChildren();
      const api = renderDiff(view, c, {
        rev: page.rev,
        findings,
        fold: fc.fold,
        reveal,
        scrollTo: (el) => scrollViewTo(el),
        onAnnotate: (anchor, text, kind, blocking) =>
          addFinding(page, anchor, text, kind, blocking),
        onFoldChange: () => syncExpand(),
      });
      if (!reveal) restoreScroll(view);
      // Head: totals + expand/collapse-all + the since-rev note.
      if (actions) {
        const st = diffStats(c.files || []);
        const btn = h("button", { class: "whbtn",
          onclick: () => { api.expandAll(!api.allOpen); } });
        const syncLabel = () =>
          (btn.textContent = api.allOpen ? "collapse all" : "expand all");
        expandSync = syncLabel;
        syncLabel();
        actions.replaceChildren(
          h("span", { class: "micro" },
            `${st.files} file${st.files === 1 ? "" : "s"} · `,
            h("span", { class: "add" }, "+" + st.add), " ",
            h("span", { class: "del" }, "−" + st.del)),
          btn);
      }
      if (srnote && c.interdiff) {
        const inter = c.interdiff;
        const moved = inter.changed.length + inter.added.length
          + inter.removed.length;
        srnote.textContent =
          `${moved} file${moved === 1 ? "" : "s"} changed since r${inter.since_rev}`;
        if (inter.removed.length)
          srnote.title = "removed: " + inter.removed.join(", ");
      }
    } catch (e) { view.textContent = "could not load: " + errMsg(e); }
    return;
  }
  if (page.type === "terminal") {
    try {
      const c = await fetchContent(page.page_id, page.rev);
      // Head actions (mockup): mode truth + kill for daemon-spawned ones.
      if (actions) {
        const mode = (c && c.mode) || "mirror";
        actions.replaceChildren(
          h("span", { class: "micro", style: "color:var(--ok)" },
            mode === "stream" ? "live · ring replay" : "mirror · tmux"));
        const handle = c && c.handle;
        if (handle)
          actions.append(h("button", { class: "whbtn danger",
            onclick: async () => {
              const yes = await confirmDanger(`kill ${handle}?`,
                "the process dies with it — this is not undoable", "kill");
              if (!yes) return;
              try { await bus.command("session.kill", { name: handle }); }
              catch (e) { toast("kill failed: " + errMsg(e), true); }
            } }, "kill"));
      }
      await renderTerminal(view, page, c, {
        wb: (window.__VOCO__ || {}).wb || "",
        command: (cmd, payload) => bus.command(cmd, payload),
      });
    } catch (e) { view.textContent = "terminal unavailable: " + errMsg(e); }
    return;
  }
  view.classList.add("empty");
  view.textContent = `${page.type} pages arrive in a later slice`;
}

// The expand-all button's label lives across renders via this seam.
let expandSync = () => {};
function syncExpand() { expandSync(); }

async function addFinding(page, anchor, text, kind, blocking) {
  try {
    await bus.command("finding.add", {
      workspace: store.selectedWorkspace, page_id: page.page_id,
      anchor, text, kind, blocking: !!blocking,
    });
  } catch (e) { toast("annotation failed: " + errMsg(e), true); }
}

async function closePage(p) {
  try { await bus.command("page.close", { page_id: p.page_id }); }
  catch (e) { toast("close failed: " + errMsg(e), true); }
}

// ---- dock: scope header + annotations | transcript ------------------------------
let dockTab = "annotations";

function setDockTab(name) { dockTab = name; renderDock(); }

function renderDock() {
  const scroller = dock.querySelector(".dock-scroll");
  const keep = scroller ? scroller.scrollTop : 0;
  dock.replaceChildren();
  const wsKey = store.selectedWorkspace || "";
  const ws = store.selectedWs();
  const agent = selectedAgent();

  const scope = h("div", { class: "dock-scope" });
  if (agent) scope.append(h("span", { class: "who" }, agent.name));
  if (ws) {
    scope.append(h("span", { class: "where" },
      (agent ? " · " : "") + (ws.repo || ws.name)
      + (ws.branch ? " · " + ws.branch : "")));
    for (const c of linkChips(ws)) if (c) scope.append(" ", c);
    if (!agent && !agentsIn(ws).length)
      scope.append(h("span", { class: "where" }, " · no agent"));
  }
  if (!agent && !ws) scope.append(h("span", { class: "where" }, "nothing selected"));
  dock.append(scope);

  const openCount = store.findingsFor(wsKey)
    .filter((f) => f.status === "open").length;
  const tab = (name, label) => h("div",
    { class: "dock-tab" + (dockTab === name ? " on" : ""),
      onclick: () => setDockTab(name) }, label);
  const tabs = h("div", { class: "dock-tabs" },
    tab("annotations", openCount ? `annotations (${openCount})` : "annotations"),
    tab("transcript", "transcript"),
    h("button", { class: "dock-export", onclick: () => exportReview() }, "export ↓"));
  dock.append(tabs);

  const body = h("div", { class: "dock-body" });
  dock.append(body);
  if (dockTab === "transcript") {
    if (agent) {
      renderTranscript(body, store.transcriptFor(agent.session_id),
        { agentName: agent.name, speaking: store.speaking });
      loadTranscript(agent); // refetch if stale
    } else {
      const n = ws ? agentsIn(ws).length : 0;
      body.append(h("div", { class: "empty-note" }, n
        ? "click an agent in the rail to focus its conversation"
        : "no agent attached here — transcripts appear when one connects"));
    }
    return;
  }
  const diffOpen = (ws?.pages || []).some((p) => p.type === "diff" && !p.closed);
  const items = [
    ...store.findingsFor(wsKey),
    // asks render in the same ledger: a question is an annotation
    // without a line (findings.mjs shows them via the answer field).
  ];
  renderFindings(body, items, {
    restoreScroll: keep,
    emptyText: diffOpen
      ? "no annotations — click a diff line to flag one"
      : "no annotations — publish a diff first, then click a line to flag one",
    onWithdraw: async (id) => {
      try {
        await bus.command("finding.withdraw", { workspace: wsKey, finding_id: id });
        // Undo-over-confirm: withdraw is reversible, so no dialog —
        // the toast carries the way back (re-open via finding.status).
        toastUndo("annotation withdrawn —", async () => {
          try {
            await bus.command("finding.status",
              { workspace: wsKey, finding_id: id, status: "open" });
          } catch (e) { toast("undo failed: " + errMsg(e), true); }
        });
      } catch (e) { toast("withdraw failed: " + errMsg(e), true); }
    },
    onReveal: (f) => revealFinding(f),
    pageRev: (pageId) => {
      const w = store.workspaces.get(wsKey);
      const p = w && w.pages.find((x) => x.page_id === pageId);
      return p ? p.rev : null;
    },
  });
}

async function loadTranscript(agent) {
  const t = store.transcriptFor(agent.session_id);
  if (t && !t.stale) return;
  try {
    const data = await bus.command("session.transcript", { name: agent.name });
    store.setTranscript(agent.session_id, data);
  } catch (e) { /* offline or old daemon; the tab shows the empty note */ }
}

function revealFinding(f) {
  const a = f.anchor || {};
  // Route through the reveal seam so a collapsed fold opens first; the
  // page re-render is async (content fetch), so the blink retries.
  if (a.file) pendingReveal = { pageId: f.page_id, path: a.file };
  store.selectPage(f.page_id);
  blinkRow(a);
}

function blinkRow(a, tries = 40) {
  const sel = `.drow[data-file="${cssEscape(a.file)}"][data-side="${a.side}"][data-line="${a.startLine}"]`;
  const row = work.querySelector(sel);
  if (row) {
    scrollViewTo(row, "center"); // container-scoped: never yanks ancestors
    row.classList.add("blink");
    return;
  }
  if (tries > 0) requestAnimationFrame(() => blinkRow(a, tries - 1));
}
const cssEscape = (s) => String(s).replace(/["\\]/g, "\\$&");

async function exportReview() {
  try {
    const r = await bus.command("review.export", { workspace: store.selectedWorkspace });
    toast(`exported ${r.count} annotation(s) → ${r.out}`);
  } catch (e) { toast("export failed: " + errMsg(e), true); }
}

// Fetch full findings + asks when work is selected (the snapshot
// carries counts only).
async function loadFindings(wsKey) {
  if (!wsKey || store.findings.has(wsKey)) return;
  try {
    const r = await bus.command("finding.list", { workspace: wsKey });
    const m = new Map();
    for (const f of r.findings || []) m.set(f.finding_id, f);
    store.findings.set(wsKey, m);
    store._notify("findings");
  } catch (e) { /* not connected yet; a later event fills it */ }
}

async function loadAsks(wsKey) {
  if (!wsKey || store.asks.has(wsKey)) return;
  try {
    const r = await bus.command("ask.list", { workspace: wsKey });
    const m = new Map();
    for (const a of r.asks || []) m.set(a.ask_id, a);
    store.asks.set(wsKey, m);
    store._notify("asks");
  } catch (e) { /* not connected yet; a later event fills it */ }
}

// ---- "full": the transcript IS the expansion ----------------------------------
function jumpToTranscript(target) {
  setDockTab("transcript");
  requestAnimationFrame(() => flashEntry(dock, target));
}

// ---- presence strip -------------------------------------------------------------
function renderStrip() {
  renderPresence(presence, store, {
    command: (cmd, payload) => bus.command(cmd, payload),
    onFull: jumpToTranscript,
    toast,
    onSettings: () => openSettings(mctx()),
  });
}

// ---- status line: ambient truth ---------------------------------------------
function renderStatus() {
  const mic = store.mic || {};
  const active = store.activeSession && store.sessions.get(store.activeSession);
  const counts = { blocked: 0, working: 0, listening: 0 };
  for (const s of store.sessions.values()) {
    const st = stateOf(s);
    if (st in counts) counts[st]++;
  }
  const openCount = store.findingsFor(store.selectedWorkspace || "")
    .filter((f) => f.status === "open").length;
  statusline.replaceChildren(
    h("span", { class: "conn-cell " + (store.connected ? "on" : "off") },
      store.connected ? "● " + location.host : "○ reconnecting"),
    h("span", {}, active ? "mic → " + active.name : "mic → nobody"),
    h("span", {}, [mic.attention, mic.duplex].filter(Boolean).join(" · ") || "headless"),
    h("span", { class: "spacer" }),
    h("span", {},
      counts.blocked ? h("span", { class: "bad" }, counts.blocked + " blocked · ") : "",
      h("span", { class: "warn" }, counts.working + " working"),
      " · " + counts.listening + " listening"),
    openCount ? h("span", { class: "amber" }, openCount + " open") : "");
}

// ---- disconnected: a designed state -------------------------------------------
function renderConn() {
  body.classList.toggle("offline", !store.connected);
  renderStrip(); renderStatus();
  if (store.connected) {
    loadFindings(store.selectedWorkspace || "");
    loadAsks(store.selectedWorkspace || "");
    const agent = selectedAgent();
    if (agent) loadTranscript(agent);
  }
}

// ---- keyboard floor -------------------------------------------------------------
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    document.querySelectorAll(".annot-editor").forEach((n) => n.remove());
    // the editor's range highlight dies with it (self-review: Esc used
    // to strand .selected rows)
    document.querySelectorAll(".drow.selected")
      .forEach((n) => n.classList.remove("selected"));
  }
});

// ---- panel resize (persisted) ----------------------------------------------------
function grip(el, cssVar, min, max, fromRight, storeKey) {
  const saved = localStorage.getItem(storeKey);
  if (saved) body.style.setProperty(cssVar, saved + "px");
  el.addEventListener("pointerdown", (down) => {
    down.preventDefault();
    el.setPointerCapture(down.pointerId);
    el.classList.add("dragging");
    const start = down.clientX;
    const startW = parseFloat(getComputedStyle(body).getPropertyValue(cssVar));
    const move = (mv) => {
      const d = fromRight ? start - mv.clientX : mv.clientX - start;
      const w = Math.min(max, Math.max(min, startW + d));
      body.style.setProperty(cssVar, w + "px");
      localStorage.setItem(storeKey, String(w));
    };
    const up = () => {
      el.classList.remove("dragging");
      el.removeEventListener("pointermove", move);
      el.removeEventListener("pointerup", up);
    };
    el.addEventListener("pointermove", move);
    el.addEventListener("pointerup", up);
  });
}
grip(gripRail, "--railw", 180, 400, false, "voco.railw");
grip(gripDock, "--dockw", 240, 520, true, "voco.dockw");

// ---- wire subscriptions -----------------------------------------------------
store.subscribe("workspaces", () => { renderRail(); renderWork(); renderDock(); });
store.subscribe("sessions", () => { renderRail(); renderWork(); renderStrip(); renderStatus(); });
store.subscribe("selection", () => {
  renderRail(); renderWork(); renderDock();
  loadFindings(store.selectedWorkspace || "");
  loadAsks(store.selectedWorkspace || "");
});
store.subscribe("findings", () => { renderDock(); renderWork(); renderRail(); renderStatus(); });
store.subscribe("asks", () => { renderDock(); renderRail(); });
store.subscribe("voice", renderStrip);
// speech.sentence fires per sentence — the strip updates every time,
// but the rail only cares WHO is speaking (eq marker), and the dock
// only when the transcript is showing (karaoke lives there).
let lastSpeakerWho = null;
store.subscribe("speaking", () => {
  renderStrip();
  const who = store.speaking && store.speaking.who;
  if (who !== lastSpeakerWho) { lastSpeakerWho = who; renderRail(); }
  if (dockTab === "transcript") renderDock();
});
store.subscribe("transcript", () => { if (dockTab === "transcript") renderDock(); });
store.subscribe("mic", () => { renderStrip(); renderStatus(); });
store.subscribe("conn", renderConn);
store.subscribe("ticker", renderStrip);

renderStrip(); renderRail(); renderWork(); renderDock(); renderStatus();
