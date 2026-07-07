// @ts-check
/**
 * Workbench entry (SPEC-WORKBENCH §7; DESIGN-DECK U1) — agent-centric.
 *
 * ONE selection: the agent. The rail is the only navigation — repo
 * groups → agents → their pages (the tree IS the scoping model; no
 * center tabs). The presence strip owns voice moments; the dock
 * (annotations | transcript) follows the selection and says so; the
 * status line carries ambient truth. Disconnected is a designed state:
 * surfaces dim read-only, the strip and status line say why.
 */

import { Store } from "./store.mjs";
import { connectBus } from "./bus.mjs";
import { renderMarkdown } from "./markdown.mjs";
import { renderDiff } from "./diff.mjs";
import { renderFindings } from "./findings.mjs";
import { renderTranscript, flashEntry } from "./transcript.mjs";
import { renderPresence } from "./presence.mjs";
import { renderTerminal } from "./term.mjs";

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
const errMsg = (e) => (e instanceof Error ? e.message : String(e));

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

// ---- the ONE selection: pick an agent ----------------------------------------
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
  try { await bus.command("switch_session", { name: s.name }); }
  catch (e) { toast("activate failed: " + errMsg(e), true); }
  if (ws && (!Array.isArray(s.capabilities) || s.capabilities.includes("review"))) {
    try { await bus.command("review.primary", { workspace: ws.key, agent: s.name }); }
    catch (e) { /* agent may lack review capability; the dock says so */ }
  }
}

async function detachAgent(s) {
  if (!confirm(`forget ${s.name}? (the agent process is not touched)`)) return;
  try { await bus.command("session.detach", { name: s.name }); }
  catch (e) { toast("detach failed: " + errMsg(e), true); }
}

// ---- rail: repo groups → agents → pages ---------------------------------------
const PAGE_ICON = { screen: "▦", diff: "±", doc: "¶", terminal: "❯" };

function groupKey(ws) { return ws.common_dir || ws.key; }

function renderRail() {
  rail.replaceChildren();
  // Group workspaces by repo (common_dir folds worktree siblings).
  /** @type {Map<string, {name:string, spaces:any[]}>} */
  const groups = new Map();
  for (const ws of store.workspaces.values()) {
    const key = groupKey(ws);
    if (!groups.has(key))
      groups.set(key, { name: ws.repo || ws.name, spaces: [] });
    const g = /** @type {{name:string, spaces:any[]}} */ (groups.get(key));
    g.spaces.push(ws);
    if (ws.repo) g.name = ws.repo;
  }
  const placed = new Set();
  for (const [key, g] of groups) {
    rail.append(h("div", { class: "repo-group" },
      h("span", { class: "rname" }, g.name)));
    // Agents whose home is any workspace in this group, blocked first.
    const agents = [...store.sessions.values()]
      .filter((s) => g.spaces.some((ws) => sessionInWs(s, ws)))
      .sort((a, b) =>
        (STATE_ORDER[stateOf(a)] ?? 9) - (STATE_ORDER[stateOf(b)] ?? 9)
        || a.name.localeCompare(b.name));
    for (const s of agents) { placed.add(s.session_id); rail.append(agentRow(s)); }
    if (!agents.length)
      rail.append(h("div", { class: "rail-note" }, "no agents"));
  }
  // Sessions with no known workspace yet (pre-identity registrations).
  const stray = [...store.sessions.values()].filter((s) => !placed.has(s.session_id));
  if (stray.length) {
    rail.append(h("div", { class: "repo-group" },
      h("span", { class: "rname" }, "elsewhere")));
    for (const s of stray) rail.append(agentRow(s));
  }
  if (!store.sessions.size && !store.workspaces.size)
    rail.append(h("div", { class: "empty-note" },
      "no agents — run voice_init (MCP) or `voco listen` in a repo"));
}

function agentRow(s) {
  const ws = wsOf(s);
  const sel = store.selectedAgent === s.session_id;
  const row = h("div", { class: "agent-row" + (sel ? " sel" : ""),
    onclick: () => selectAgent(s) },
    h("div", { class: "ar-top" },
      dot(s),
      h("span", { class: "ar-name" }, s.name),
      store.activeSession === s.session_id
        ? h("span", { class: "bolt", title: "voice-active" }, "⚡") : "",
      store.speaking && store.speaking.who === s.name ? speakingEq() : "",
      h("span", { class: "ar-state " + stateOf(s) }, stateOf(s)),
      h("span", { class: "rail-x", title: "forget this session",
        onclick: (e) => { e.stopPropagation(); detachAgent(s); } }, "✕")),
    h("div", { class: "ar-sub" },
      h("span", {}, ws ? (ws.branch || ws.name) : "—"),
      flaggedChip(ws),
      s.queued ? h("span", { class: "chip hot" }, s.queued + " queued") : ""));
  if (sel) return h("div", {}, row, pagesTree(s, ws));
  return row;
}

function speakingEq() {
  const eq = h("span", { class: "eq", title: "speaking aloud" });
  for (let i = 0; i < 3; i++) eq.append(h("i"));
  return eq;
}

function flaggedChip(ws) {
  if (!ws) return "";
  const open = store.findingsFor(ws.key)
    .filter((f) => f.status === "open").length
    + store.asksFor(ws.key).filter((a) => a.answer == null).length;
  return open ? h("span", { class: "chip amber" }, open + " flagged") : "";
}

function pagesTree(s, ws) {
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
      p.rev > 1 ? h("span", { class: "rev" }, "r" + p.rev) : "");
    if (!p.pinned)
      row.append(h("span", { class: "rail-x", title: "close page",
        onclick: (e) => { e.stopPropagation(); closePage(p); } }, "✕"));
    tree.append(row);
  }
  return tree;
}

// ---- work: crumb header + view -------------------------------------------------
function renderWork() {
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
  if (agent) crumb.append(h("span", { class: "crumb-who" }, agent.name),
    h("span", { class: "sep" }, " / "));
  crumb.append(h("span", {}, page ? page.title : "overview"));
  if (page && page.rev > 1)
    crumb.append(h("span", { class: "sep" }, " · "),
      h("span", { class: "micro rev" }, "r" + page.rev));
  work.append(h("div", { class: "work-head" }, crumb));
  const view = h("div", { class: "view" });
  work.append(view);
  if (page) { renderPage(view, page); return; }
  if (agent) { renderAgentCard(view, agent, ws); return; }
  view.classList.add("empty");
  view.append(h("div", { class: "empty-note" }, "no pages here yet"));
}

function renderAgentCard(view, s, ws) {
  const card = h("div", { class: "agent-card" });
  card.append(h("div", { class: "agent-card-head" },
    dot(s),
    h("span", { class: "agent-card-name" }, s.display_name || s.name),
    store.activeSession === s.session_id
      ? h("span", { class: "agent-card-active" }, "⚡ voice-active") : ""));
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

function welcomeView() {
  return h("div", { class: "agent-card" },
    h("div", { class: "agent-card-name" }, "voco deck"),
    h("p", {}, "No agents attached yet. In an agent session (Claude Code, "
      + "Codex…) call the voice_init MCP tool, or run `voco listen` from "
      + "a shell in your repo — the agent appears in the rail."));
}

const kv = (k, v) => h("span", { class: "st-item" },
  h("span", { class: "k" }, k), h("span", {}, v));

async function renderPage(view, page) {
  if (page.type === "screen" || page.type === "doc") {
    view.textContent = "…";
    try {
      const c = await fetchContent(page.page_id, page.rev);
      view.replaceChildren();
      await renderMarkdown(view, c.markdown || "");
    } catch (e) { view.textContent = "could not load: " + errMsg(e); }
    return;
  }
  if (page.type === "diff") {
    view.textContent = "…";
    try {
      const c = await fetchContent(page.page_id, page.rev);
      view.replaceChildren();
      renderDiff(view, c, {
        rev: page.rev,
        findings: store.findingsFor(store.selectedWorkspace || "")
          .filter((f) => f.page_id === page.page_id),
        onAnnotate: (anchor, text, kind) => addFinding(page, anchor, text, kind),
      });
    } catch (e) { view.textContent = "could not load: " + errMsg(e); }
    return;
  }
  if (page.type === "terminal") {
    try {
      const c = await fetchContent(page.page_id, page.rev);
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

async function addFinding(page, anchor, text, kind) {
  try {
    await bus.command("finding.add", {
      workspace: store.selectedWorkspace, page_id: page.page_id,
      anchor, text, kind,
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
  dock.replaceChildren();
  const wsKey = store.selectedWorkspace || "";
  const ws = store.selectedWs();
  const agent = selectedAgent();

  const scope = h("div", { class: "dock-scope" });
  if (agent) scope.append(h("span", { class: "who" }, agent.name));
  if (ws) scope.append(h("span", { class: "where" },
    (agent ? " · " : "") + ws.name + (ws.branch ? " · " + ws.branch : "")));
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
    renderTranscript(body,
      agent ? store.transcriptFor(agent.session_id) : null,
      { agentName: agent ? agent.name : null, speaking: store.speaking });
    if (agent) loadTranscript(agent); // refetch if stale
    return;
  }
  const diffOpen = (ws?.pages || []).some((p) => p.type === "diff" && !p.closed);
  const items = [
    ...store.findingsFor(wsKey),
    // asks render in the same ledger: a question is an annotation
    // without a line (findings.mjs shows them via the answer field).
  ];
  renderFindings(body, items, {
    emptyText: diffOpen
      ? "no annotations — click a diff line to flag one"
      : "no annotations — publish a diff first, then click a line to flag one",
    onWithdraw: async (id) => {
      try {
        await bus.command("finding.withdraw", { workspace: wsKey, finding_id: id });
        toast("withdrawn");
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
  const ws = store.selectedWs();
  if (ws && f.page_id !== store.selectedPage) store.selectPage(f.page_id);
  requestAnimationFrame(() => {
    const a = f.anchor || {};
    const sel = `.drow[data-file="${cssEscape(a.file)}"][data-side="${a.side}"][data-line="${a.startLine}"]`;
    const row = work.querySelector(sel);
    if (row) { row.scrollIntoView({ block: "center" }); row.classList.add("blink"); }
  });
}
const cssEscape = (s) => String(s).replace(/["\\]/g, "\\$&");

async function exportReview() {
  try {
    const r = await bus.command("review.export", { workspace: store.selectedWorkspace });
    toast(`exported ${r.count} annotation(s) → ${r.out}`);
  } catch (e) { toast("export failed: " + errMsg(e), true); }
}

// Fetch full findings + asks when a workspace is selected (the snapshot
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
    active ? h("span", {}, active.name + " active") : h("span", {}, "no agent"),
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
  if (e.key === "Escape")
    document.querySelectorAll(".annot-editor").forEach((n) => n.remove());
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
store.subscribe("workspaces", () => { renderRail(); renderWork(); });
store.subscribe("sessions", () => { renderRail(); renderWork(); renderStrip(); renderStatus(); });
store.subscribe("selection", () => {
  renderRail(); renderWork(); renderDock();
  loadFindings(store.selectedWorkspace || "");
  loadAsks(store.selectedWorkspace || "");
});
store.subscribe("findings", () => { renderDock(); renderWork(); renderRail(); renderStatus(); });
store.subscribe("asks", () => { renderDock(); renderRail(); });
store.subscribe("voice", renderStrip);
store.subscribe("speaking", () => { renderStrip(); renderRail(); renderDock(); });
store.subscribe("transcript", () => { if (dockTab === "transcript") renderDock(); });
store.subscribe("mic", () => { renderStrip(); renderStatus(); });
store.subscribe("conn", renderConn);
store.subscribe("ticker", renderStrip);

renderStrip(); renderRail(); renderWork(); renderDock(); renderStatus();
