// @ts-check
/**
 * Workbench entry (SPEC-WORKBENCH §7). Builds the rail/editor/dock/status
 * skeleton, wires the store to the WS bus, and mounts the region renderers.
 * The region functions are the seam W1+ extends (findings dock, diff view).
 */

import { Store } from "./store.mjs";
import { connectBus } from "./bus.mjs";
import { renderMarkdown } from "./markdown.mjs";
import { renderDiff } from "./diff.mjs";
import { renderFindings } from "./findings.mjs";
import { renderChat } from "./chat.mjs";
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
    el.append(kid instanceof Node ? kid : document.createTextNode(kid));
  return el;
};

// ---- shell ------------------------------------------------------------------
const app = /** @type {HTMLElement} */ (document.getElementById("app"));
const rail = h("div", { class: "rail" });
const editor = h("div", { class: "editor" });
const dock = h("div", { class: "dock" });
const statusbar = h("div", { class: "statusbar" });
const workbench = h("div", { class: "workbench" }, rail, editor, dock);
app.append(workbench, statusbar);

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

// ---- rail: repos -> workspaces -> agents; roster below ----------------------
function renderRail() {
  rail.replaceChildren();
  const spaces = [...store.workspaces.values()];
  // Group by repo common_dir (worktree siblings) then loose sessionspaces.
  const groups = new Map();
  for (const ws of spaces) {
    const g = ws.common_dir || ws.repo || (ws.kind === "sessionspace" ? "· no repo" : ws.key);
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(ws);
  }
  const top = h("div", { class: "rail-section" },
    h("div", { class: "rail-head" }, h("span", {}, "workspaces")));
  for (const [g, list] of groups) {
    const label = list[0].repo || (list[0].kind === "sessionspace" ? "no repo" : g);
    const repoRow = h("div", { class: "rail-item rail-repo" },
      h("span", {}, "▸ " + label));
    if (list[0].kind === "workspace")
      repoRow.append(h("span", {
        class: "rail-add", title: "spawn an agent in a new worktree",
        onclick: (e) => { e.stopPropagation(); spawnWorktree(list[0]); },
      }, "+"));
    top.append(repoRow);
    for (const ws of list) {
      const sel = ws.key === store.selectedWorkspace ? " sel" : "";
      const branch = ws.branch ? h("span", { class: "rail-branch" }, ws.branch) : "";
      const nPages = ws.pages.filter((p) => !p.closed).length;
      const item = h("div",
        { class: "rail-item indent" + sel, onclick: () => store.selectWorkspace(ws.key) },
        h("span", {}, ws.name), branch);
      if (nPages) item.append(h("span", { class: "count-chip" }, String(nPages)));
      top.append(item);
      for (const s of agentsIn(ws))
        top.append(h("div", { class: "rail-item indent2" },
          dot(s), h("span", {}, s.name)));
    }
  }
  rail.append(top);

  // Flat agent roster (presence + quick-switch).
  const roster = h("div", { class: "rail-section" },
    h("div", { class: "rail-head" }, h("span", {}, "agents")));
  for (const s of store.sessions.values())
    roster.append(h("div",
      { class: "rail-item", onclick: () => quickSwitch(s) },
      dot(s), h("span", {}, s.display_name || s.name)));
  if (!store.sessions.size)
    roster.append(h("div", { class: "empty-note" }, "no agents attached"));
  rail.append(roster);
}

function agentsIn(ws) {
  // A session is "in" a workspace when it owns an agent-scoped page there.
  const names = new Set(ws.pages.filter((p) => p.call_name).map((p) => p.call_name));
  return [...store.sessions.values()].filter((s) => names.has(s.name));
}

// W3: spawn an agent in a fresh sibling worktree of this repo.
async function spawnWorktree(ws) {
  const branch = prompt("new worktree branch:");
  if (!branch) return;
  const harness = prompt("harness command:", "claude");
  if (!harness) return;
  try {
    const r = await bus.command("session.spawn",
      { harness, cwd: ws.root, worktree: { branch } });
    toast(`spawned ${r.tmux_session} in ${r.worktree || ws.root}`);
  } catch (e) {
    toast("spawn failed: " + (e instanceof Error ? e.message : e));
  }
}

function quickSwitch(s) {
  for (const ws of store.workspaces.values())
    if (ws.pages.some((p) => p.call_name === s.name)) {
      store.selectWorkspace(ws.key);
      const term = ws.pages.find((p) => p.type === "terminal" && p.call_name === s.name);
      if (term) store.selectPage(term.page_id);
      return;
    }
}

function dot(s) {
  const state = s.display_state || s.state || "idle";
  return h("span", { class: "dot " + state, title: state });
}

// ---- editor: tabstrip + active view ----------------------------------------
function renderEditor() {
  editor.replaceChildren();
  const ws = store.selectedWs();
  if (!ws) {
    editor.append(h("div", { class: "view empty" }, "select a workspace"));
    return;
  }
  const rank = (p) => (p.pinned ? 0 : 1);
  const pages = ws.pages.filter((p) => !p.closed)
    .sort((a, b) => rank(a) - rank(b) || a.page_id.localeCompare(b.page_id));
  const strip = h("div", { class: "tabstrip" });
  for (const p of pages) {
    const on = p.page_id === store.selectedPage ? " on" : "";
    const tab = h("div", { class: "tab" + on, onclick: () => store.selectPage(p.page_id) },
      p.pinned ? h("span", { class: "pin" }, "◆") : "",
      h("span", {}, p.title),
      p.rev > 1 ? h("span", { class: "rev" }, "r" + p.rev) : "");
    if (!p.pinned)
      tab.append(h("span", { class: "x", onclick: (e) => { e.stopPropagation(); closePage(p); } }, "✕"));
    strip.append(tab);
  }
  editor.append(strip);

  const view = h("div", { class: "view" });
  editor.append(view);
  const page = pages.find((p) => p.page_id === store.selectedPage);
  if (!page) { view.classList.add("empty"); view.textContent = "no page open"; return; }
  renderPage(view, page);
}

async function renderPage(view, page) {
  if (page.type === "screen" || page.type === "doc") {
    view.textContent = "…";
    try {
      const c = await fetchContent(page.page_id, page.rev);
      view.replaceChildren();
      await renderMarkdown(view, c.markdown || "");
    } catch (e) {
      view.textContent = "could not load: " + (e instanceof Error ? e.message : e);
    }
    return;
  }
  if (page.type === "diff") {
    view.textContent = "…";
    try {
      const c = await fetchContent(page.page_id, page.rev);
      view.replaceChildren();
      renderDiff(view, c, {
        findings: store.findingsFor(store.selectedWorkspace || "")
          .filter((f) => f.page_id === page.page_id),
        onAnnotate: (anchor, text, kind) => addFinding(page, anchor, text, kind),
      });
    } catch (e) {
      view.textContent = "could not load: " + (e instanceof Error ? e.message : e);
    }
    return;
  }
  if (page.type === "terminal") {
    try {
      const c = await fetchContent(page.page_id, page.rev);
      await renderTerminal(view, page, c, {
        wb: (window.__VOCO__ || {}).wb || "",
        command: (cmd, payload) => bus.command(cmd, payload),
      });
    } catch (e) {
      view.textContent = "terminal unavailable: "
        + (e instanceof Error ? e.message : e);
    }
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
  } catch (e) { console.error("finding.add", e); }
}

async function closePage(p) {
  try { await bus.command("page.close", { page_id: p.page_id }); }
  catch (e) { console.error(e); }
}

// ---- dock: findings + chat tabs (SPEC-WORKBENCH §4) --------------------------
let dockTab = "findings";

function renderDock() {
  dock.replaceChildren();
  const wsKey = store.selectedWorkspace || "";
  const tab = (name, label) => h("div",
    { class: "dock-tab" + (dockTab === name ? " on" : ""),
      onclick: () => { dockTab = name; renderDock(); } },
    label);
  const pendingAsks = store.asksFor(wsKey).filter((a) => a.answer == null).length;
  dock.append(h("div", { class: "dock-tabs" },
    tab("findings", "findings"),
    tab("chat", pendingAsks ? `chat (${pendingAsks})` : "chat"),
    h("div", { class: "dock-tab", onclick: () => exportReview() }, "export ↓")));
  const body = h("div", { class: "dock-body" });
  dock.append(body);
  if (dockTab === "chat") {
    renderChat(body, store.asksFor(wsKey), {
      hasReviewAgent: hasReviewAgent(store.selectedWs()),
      onAsk: async (text) => {
        try { await bus.command("ask.create", { workspace: wsKey, text }); }
        catch (e) { toast("ask failed: " + (e instanceof Error ? e.message : e)); }
      },
    });
    return;
  }
  renderFindings(body, store.findingsFor(wsKey), {
    onWithdraw: async (id) => {
      try { await bus.command("finding.withdraw", { workspace: wsKey, finding_id: id }); }
      catch (e) { console.error(e); }
    },
    onReveal: (f) => revealFinding(f),
  });
}

// Is a review-capable agent attached to THIS workspace (§4.3)? Session
// home identity (host/root) rides the snapshot and session.attached.
// Unknowable facts count in the agent's favor — never falsely claim
// absence for a session we can't place.
function hasReviewAgent(ws) {
  if (!ws) return false;
  const strip = (p) => String(p || "").replace(/\/+$/, "");
  return [...store.sessions.values()].some((s) => {
    const capOk = !Array.isArray(s.capabilities)
      || s.capabilities.includes("review");
    const placeKnown = s.host != null && s.root != null;
    const placeOk = !placeKnown
      || (s.host === ws.host && strip(s.root) === strip(ws.root));
    return capOk && placeOk;
  });
}

function revealFinding(f) {
  const ws = store.selectedWs();
  if (ws && f.page_id !== store.selectedPage) store.selectPage(f.page_id);
  // After the view renders, scroll the anchored line into view.
  requestAnimationFrame(() => {
    const a = f.anchor || {};
    const sel = `.drow[data-file="${cssEscape(a.file)}"][data-side="${a.side}"][data-line="${a.startLine}"]`;
    const row = editor.querySelector(sel);
    if (row) { row.scrollIntoView({ block: "center" }); row.classList.add("blink"); }
  });
}
const cssEscape = (s) => String(s).replace(/["\\]/g, "\\$&");

async function exportReview() {
  try {
    const r = await bus.command("review.export", { workspace: store.selectedWorkspace });
    toast(`exported ${r.count} finding(s) → ${r.out}`);
  } catch (e) { toast("export failed: " + (e instanceof Error ? e.message : e)); }
}

function toast(msg) {
  const t = h("div", { class: "toast-msg" }, msg);
  document.body.append(t);
  setTimeout(() => t.remove(), 4000);
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

// ---- status bar -------------------------------------------------------------
function renderStatus() {
  const mic = store.mic || {};
  const active = store.activeSession && store.sessions.get(store.activeSession);
  statusbar.replaceChildren(
    stItem("mic", mic.duplex || "—"),
    stItem("attn", mic.attention || "—"),
    stItem("active", active ? active.name : "none"),
    h("span", { class: "ticker" }, store.ticker || ""),
    h("span", { class: "st-conn " + (store.connected ? "on" : "off") },
      store.connected ? "● live" : "○ offline"));
}
const stItem = (k, v) => h("span", { class: "st-item" },
  h("span", { class: "k" }, k), h("span", {}, String(v)));

// ---- wire subscriptions -----------------------------------------------------
store.subscribe("workspaces", renderRail);
store.subscribe("sessions", renderRail);
store.subscribe("selection", () => {
  renderRail(); renderEditor(); renderDock();
  loadFindings(store.selectedWorkspace || "");
  loadAsks(store.selectedWorkspace || "");
});
store.subscribe("findings", () => { renderDock(); renderEditor(); });
store.subscribe("asks", renderDock);
store.subscribe("mic", renderStatus);
store.subscribe("conn", () => {
  renderStatus();
  if (store.connected) {
    loadFindings(store.selectedWorkspace || "");
    loadAsks(store.selectedWorkspace || "");
  }
});
store.subscribe("ticker", renderStatus);

renderRail(); renderEditor(); renderDock(); renderStatus();
