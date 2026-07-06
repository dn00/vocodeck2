// @ts-check
/**
 * Workbench entry (SPEC-WORKBENCH §7) — agent-centric.
 *
 * ONE selection: the agent. Clicking an agent makes it voice-active,
 * opens its workspace's pages (its own screen/terminal first), and pins
 * chat to it (review.primary). Workspaces are a secondary browse list.
 * The bottom strip is the deck's heartbeat: transcripts, routing, agent
 * says, plus a type-as-user box. Operator controls (duplex, attention,
 * interrupt, detach) are ported from the proven debug UI.
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
    else if (k === "onchange") el.addEventListener("change", v);
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
const feed = h("div", { class: "voicefeed" });
const statusbar = h("div", { class: "statusbar" });
const workbench = h("div", { class: "workbench" }, rail, editor, dock);
app.append(workbench, feed, statusbar);

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

// A session is "in" a workspace when its home identity (host/root, rides
// the snapshot + session.attached) matches; owning an agent-scoped page
// there also counts (pre-identity sessions).
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

function dot(s) {
  const state = s.display_state || s.state || "idle";
  return h("span", { class: "dot " + state, title: state });
}

// ---- the ONE selection: pick an agent ----------------------------------------
async function selectAgent(s) {
  store.selectedAgent = s.session_id;
  const ws = wsOf(s);
  store.selectedWorkspace = ws ? ws.key : null;
  // Land on the agent's own board (screen, then terminal); with no own
  // pages, the agent card (null) is home — never a random page.
  const open = ws ? ws.pages.filter((p) => !p.closed) : [];
  const own = open.find((p) => p.call_name === s.name && p.type === "screen")
    || open.find((p) => p.call_name === s.name);
  store.selectedPage = own ? own.page_id : null;
  store._notify("selection");
  try { await bus.command("switch_session", { name: s.name }); }
  catch (e) { toast("activate failed: " + errMsg(e)); }
  if (ws && (!Array.isArray(s.capabilities) || s.capabilities.includes("review"))) {
    try { await bus.command("review.primary", { workspace: ws.key, agent: s.name }); }
    catch (e) { /* agent may lack review capability; chat panel says so */ }
  }
}

async function detachAgent(s) {
  if (!confirm(`forget ${s.name}? (the agent process is not touched)`)) return;
  try { await bus.command("session.detach", { name: s.name }); }
  catch (e) { toast("detach failed: " + errMsg(e)); }
}

const errMsg = (e) => (e instanceof Error ? e.message : String(e));

// ---- rail: agents first, workspaces to browse --------------------------------
function renderRail() {
  rail.replaceChildren();

  const agents = h("div", { class: "rail-section" },
    h("div", { class: "rail-head" }, h("span", {}, "agents")));
  for (const s of store.sessions.values()) {
    const ws = wsOf(s);
    const sel = store.selectedAgent === s.session_id ? " sel" : "";
    const row = h("div", { class: "rail-agent" + sel, onclick: () => selectAgent(s) },
      h("div", { class: "rail-agent-top" },
        dot(s),
        h("span", { class: "rail-agent-name" },
          (store.activeSession === s.session_id ? "⚡ " : "") + s.name),
        h("span", { class: "rail-state" }, s.display_state || s.state || ""),
        h("span", {
          class: "rail-x", title: "forget this session",
          onclick: (e) => { e.stopPropagation(); detachAgent(s); },
        }, "✕")),
      h("div", { class: "rail-agent-sub" },
        h("span", {}, ws ? ws.name + (ws.branch ? " · " + ws.branch : "") : "—"),
        s.unread_digest
          ? h("span", { class: "count-chip" }, String(s.unread_digest)) : "",
        s.queued ? h("span", { class: "count-chip hot" }, s.queued + " queued") : ""));
    agents.append(row);
  }
  if (!store.sessions.size)
    agents.append(h("div", { class: "empty-note" },
      "no agents — run `voco init` (MCP: voice_init) in an agent session"));
  rail.append(agents);

  // Secondary: browse ALL workspaces (incl. restored review state).
  const spaces = h("div", { class: "rail-section" },
    h("div", { class: "rail-head" }, h("span", {}, "workspaces")));
  for (const ws of store.workspaces.values()) {
    const sel = !store.selectedAgent && ws.key === store.selectedWorkspace ? " sel" : "";
    const nPages = ws.pages.filter((p) => !p.closed).length;
    const row = h("div", {
      class: "rail-item" + sel,
      onclick: () => {
        store.selectedAgent = null;
        store.selectWorkspace(ws.key);
      },
    },
      h("span", {}, ws.name),
      ws.branch ? h("span", { class: "rail-branch" }, ws.branch) : "");
    if (nPages) row.append(h("span", { class: "count-chip" }, String(nPages)));
    if (ws.kind === "workspace")
      row.append(h("span", {
        class: "rail-add", title: "spawn an agent in a new worktree",
        onclick: (e) => { e.stopPropagation(); spawnWorktree(ws); },
      }, "+"));
    spaces.append(row);
  }
  rail.append(spaces);
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
    toast(`spawned ${r.tmux_session || r.term} in ${r.worktree || ws.root}`);
  } catch (e) { toast("spawn failed: " + errMsg(e)); }
}

// ---- editor: tabstrip + active view ----------------------------------------
function renderEditor() {
  editor.replaceChildren();
  const ws = store.selectedWs();
  const agent = selectedAgent();
  if (!ws && !agent) {
    editor.append(welcomeView());
    return;
  }
  const pages = ws
    ? ws.pages.filter((p) => !p.closed)
      .sort((a, b) => (a.pinned ? 0 : 1) - (b.pinned ? 0 : 1)
        || a.page_id.localeCompare(b.page_id))
    : [];
  const strip = h("div", { class: "tabstrip" });
  if (agent)
    strip.append(h("div", {
      class: "tab" + (store.selectedPage == null ? " on" : ""),
      onclick: () => store.selectPage(null),
    }, h("span", {}, "◈ " + agent.name)));
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
  if (page) { renderPage(view, page); return; }
  if (agent) { renderAgentCard(view, agent, ws); return; }
  view.classList.add("empty");
  view.append(wsHelp(ws));
}

// The agent card — the home view for a selected agent (replaces the old
// dead "no page open"): who it is, what it last said, how to feed it.
function renderAgentCard(view, s, ws) {
  const card = h("div", { class: "agent-card" });
  card.append(h("div", { class: "agent-card-head" },
    dot(s),
    h("span", { class: "agent-card-name" }, s.display_name || s.name),
    store.activeSession === s.session_id
      ? h("span", { class: "agent-card-active" }, "⚡ voice-active") : ""));
  card.append(h("div", { class: "agent-card-meta" },
    kv("state", s.display_state || s.state || "?"),
    kv("queued", String(s.queued || 0)),
    kv("unread", String(s.unread_digest || 0)),
    kv("capabilities", (s.capabilities || []).join(" ") || "?")));
  const tail = s.say_tail || [];
  const says = h("div", { class: "agent-card-says" },
    h("div", { class: "rail-head" }, h("span", {}, "last said")));
  if (!tail.length)
    says.append(h("div", { class: "empty-note" }, "nothing said yet"));
  for (const line of tail.slice(-6))
    says.append(h("div", { class: "say-line" },
      h("span", { class: "say-ts" }, fmtTime(line.ts)),
      h("span", {}, line.text)));
  card.append(says);
  if (s.screen_markdown && s.screen_markdown.trim()) {
    const scr = h("div", { class: "agent-card-screen" });
    renderMarkdown(scr, s.screen_markdown);
    card.append(h("div", { class: "rail-head" }, h("span", {}, "screen")), scr);
  }
  card.append(hintBlock(ws));
  view.append(card);
}

function welcomeView() {
  return h("div", { class: "view" }, h("div", { class: "agent-card" },
    h("div", { class: "agent-card-name" }, "voco deck"),
    h("p", {}, "No agents attached yet. In an agent session (Claude Code, "
      + "Codex…) run the voco MCP `voice_init` tool, or `voco listen` from "
      + "a shell in your repo — the agent appears here."),
    hintBlock(null)));
}

function wsHelp(ws) {
  return h("div", { class: "agent-card" },
    h("div", { class: "agent-card-name" }, ws ? ws.name : ""),
    h("p", {}, "No pages in this workspace yet."),
    hintBlock(ws));
}

function hintBlock(_ws) {
  return h("div", { class: "hint-block" },
    h("div", { class: "rail-head" }, h("span", {}, "feed this surface")),
    hint("voco page diff --branch", "publish the branch diff to review"),
    hint("voco page doc NOTES.md", "publish a markdown doc"),
    hint("voice_screen (MCP) / voco screen", "the agent posts its board"),
    hint("click a diff line", "flag a finding — the agent wakes with it"));
}

const hint = (cmd, why) => h("div", { class: "hint-row" },
  h("code", {}, cmd), h("span", {}, " — " + why));

const kv = (k, v) => h("span", { class: "st-item" },
  h("span", { class: "k" }, k), h("span", {}, v));

const fmtTime = (ts) =>
  new Date((ts || 0) * 1000).toLocaleTimeString([], { hour12: false });

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
  } catch (e) { toast("finding failed: " + errMsg(e)); }
}

async function closePage(p) {
  try { await bus.command("page.close", { page_id: p.page_id }); }
  catch (e) { console.error(e); }
}

// ---- dock: findings + chat --------------------------------------------------
let dockTab = "findings";

function renderDock() {
  dock.replaceChildren();
  const wsKey = store.selectedWorkspace || "";
  const agent = selectedAgent();
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
      agentName: agent ? agent.name : null,
      hasReviewAgent: hasReviewAgent(store.selectedWs()),
      onAsk: async (text) => {
        try { await bus.command("ask.create", { workspace: wsKey, text }); }
        catch (e) { toast("ask failed: " + errMsg(e)); }
      },
    });
    return;
  }
  const diffOpen = (store.selectedWs()?.pages || [])
    .some((p) => p.type === "diff" && !p.closed);
  renderFindings(body, store.findingsFor(wsKey), {
    emptyText: diffOpen
      ? "no findings — click a diff line to flag one"
      : "no findings — publish a diff first (voco page diff --branch), "
        + "then click a line to flag one",
    onWithdraw: async (id) => {
      try { await bus.command("finding.withdraw", { workspace: wsKey, finding_id: id }); }
      catch (e) { console.error(e); }
    },
    onReveal: (f) => revealFinding(f),
    pageRev: (pageId) => {
      const ws = store.workspaces.get(wsKey);
      const p = ws && ws.pages.find((x) => x.page_id === pageId);
      return p ? p.rev : null;
    },
  });
}

// Is a review-capable agent attached to THIS workspace (§4.3)? Unknowable
// facts count in the agent's favor.
function hasReviewAgent(ws) {
  if (!ws) return false;
  return [...store.sessions.values()].some((s) => {
    const capOk = !Array.isArray(s.capabilities)
      || s.capabilities.includes("review");
    const placeKnown = s.host != null && s.root != null;
    const placeOk = !placeKnown
      || (s.host === ws.host && stripSlash(s.root) === stripSlash(ws.root));
    return capOk && placeOk;
  });
}

function revealFinding(f) {
  const ws = store.selectedWs();
  if (ws && f.page_id !== store.selectedPage) store.selectPage(f.page_id);
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
  } catch (e) { toast("export failed: " + errMsg(e)); }
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

// ---- voice feed strip (the deck's heartbeat) ---------------------------------
let feedCollapsed = false;
const FEED_LABEL = { you: "you", say: "agent", queued: "queued", route: "route" };

function renderFeed() {
  feed.replaceChildren();
  feed.classList.toggle("collapsed", feedCollapsed);
  const input = /** @type {HTMLInputElement} */ (h("input", {
    class: "feed-input", type: "text",
    placeholder: "type as user — routed like speech…",
  }));
  input.addEventListener("keydown", async (e) => {
    if (e.key !== "Enter") return;
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    try { await bus.command("say_as_user", { text }); }
    catch (err) { toast("send failed: " + errMsg(err)); }
  });
  feed.append(h("div", { class: "feed-bar" },
    h("span", {
      class: "feed-toggle",
      onclick: () => { feedCollapsed = !feedCollapsed; renderFeed(); },
    }, feedCollapsed ? "▸ voice" : "▾ voice"),
    input));
  if (feedCollapsed) return;
  const body = h("div", { class: "feed-body" });
  for (const line of store.voiceFeed) {
    body.append(h("div", { class: "feed-line kind-" + line.kind },
      h("span", { class: "say-ts" }, fmtTime(line.ts)),
      h("span", { class: "feed-kind" }, FEED_LABEL[line.kind] || line.kind),
      line.who ? h("span", { class: "feed-who" }, line.who) : "",
      h("span", { class: "feed-text" }, line.text)));
  }
  if (!store.voiceFeed.length)
    body.append(h("div", { class: "empty-note" },
      "voice activity lands here — transcripts, routing, agent replies"));
  feed.append(body);
  body.scrollTop = body.scrollHeight;
}

// ---- status bar: live state + the debug UI's controls -------------------------
function renderStatus() {
  const mic = store.mic || {};
  const active = store.activeSession && store.sessions.get(store.activeSession);
  statusbar.replaceChildren(
    micSelect("mic", ["full_duplex", "half_duplex"], mic.duplex,
      (v) => bus.command("mic.set", { duplex: v })),
    micSelect("attn", ["always", "wake", "ptt_only", "muted"], mic.attention,
      (v) => bus.command("mic.set", { attention: v })),
    h("button", {
      class: "st-btn", title: "barge-in + Escape to the active agent",
      onclick: () => bus.command("interrupt", {}).catch((e) => toast(errMsg(e))),
    }, "■ interrupt"),
    kv("active", active ? active.name : "none"),
    h("span", { class: "ticker" }, store.ticker || ""),
    h("span", { class: "st-conn " + (store.connected ? "on" : "off") },
      store.connected ? "● live" : "○ offline"));
}

function micSelect(label, options, current, apply) {
  const sel = /** @type {HTMLSelectElement} */ (h("select", { class: "st-select" }));
  sel.append(h("option", { value: "" }, label + (current ? ": " + current : ": —")));
  for (const o of options)
    if (o !== current) sel.append(h("option", { value: o }, o));
  sel.addEventListener("change", async () => {
    const v = sel.value;
    if (!v) return;
    try { await apply(v); }
    catch (e) { toast(`${label} failed: ` + errMsg(e)); renderStatus(); }
  });
  return sel;
}

// ---- wire subscriptions -----------------------------------------------------
store.subscribe("workspaces", () => { renderRail(); renderEditor(); });
store.subscribe("sessions", () => { renderRail(); renderEditor(); renderStatus(); });
store.subscribe("selection", () => {
  renderRail(); renderEditor(); renderDock();
  loadFindings(store.selectedWorkspace || "");
  loadAsks(store.selectedWorkspace || "");
});
store.subscribe("findings", () => { renderDock(); renderEditor(); });
store.subscribe("asks", renderDock);
store.subscribe("voice", renderFeed);
store.subscribe("mic", renderStatus);
store.subscribe("conn", () => {
  renderStatus();
  if (store.connected) {
    loadFindings(store.selectedWorkspace || "");
    loadAsks(store.selectedWorkspace || "");
  }
});
store.subscribe("ticker", renderStatus);

renderRail(); renderEditor(); renderDock(); renderFeed(); renderStatus();
