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
import { renderMarkdown, highlightCode } from "./markdown.mjs";
import { renderDiff, diffStats, seedFolds } from "./diff.mjs";
import { renderTranscript, flashEntry } from "./transcript.mjs";
import { renderDocView } from "./docview.mjs";
import { renderHtmlView } from "./htmlview.mjs";
import { renderPresence } from "./presence.mjs";
import { renderRack, setMicLevel } from "./rack.mjs";
import { ic, installIcons } from "./icons.mjs";
import { renderTerminal } from "./term.mjs";
import { openPicker, openRepo, openSpawn, openConnect, openSettings,
  confirmDanger } from "./modals.mjs";
import { openPalette, paletteOpen, closePalette } from "./palette.mjs";

// ---- tiny persistence helpers (client-local UI state) --------------------------
const persisted = (key, fallback) => {
  try {
    const v = JSON.parse(localStorage.getItem(key) || "null");
    return v ?? fallback;
  } catch { return fallback; }
};
const persistSet = (key, set) =>
  localStorage.setItem(key, JSON.stringify([...set]));

// Annotation is an explicit interaction mode. Normal selection/copy is
// the default; the user's last choice persists across reloads.
let annotationMode = !!persisted("voco.annotationMode", false);

const store = new Store();
// Console log tab (M6): a bounded ring of every bus event.
const eventLog = [];
// P4: daemon.error must be SEEN, not buried in the log tab — each
// distinct message gets one sticky toast; a restart loop repeating the
// same error re-toasts only after the first was dismissed.
/** @type {Map<string, HTMLElement>} */
const daemonAlerts = new Map();
function logBusEvent(env) {
  eventLog.push({ ts: env.ts || Date.now() / 1000, seq: env.seq ?? "",
    type: env.type || "?", payload: env.payload });
  if (eventLog.length > 300) eventLog.shift();
  if (env.type === "daemon.error") {
    const msg = "daemon: " + String(env.payload?.error || "unknown error");
    const live = daemonAlerts.get(msg);
    if (!live || !live.isConnected) daemonAlerts.set(msg, toast(msg, true));
  }
  if (env.type === "ask.answered" && dockTab !== "asks") {
    askPulse = true; // an answer landed unseen — pulse the asks tab (A5)
    renderDock();
  }
  if (dockTab === "log") renderDock();
}
const bus = connectBus(store, { onEvent: logBusEvent });

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
installIcons(); // the SVG symbol sheet — before any ic() render

const presence = h("div", { class: "cmd" });
const rail = h("div", { class: "rail" });
const gripRail = h("div", { class: "grip", role: "separator", tabindex: "0",
  "aria-label": "resize rail" });
const work = h("div", { class: "work" });
const gripDock = h("div", { class: "grip", role: "separator", tabindex: "0",
  "aria-label": "resize ledger" });
// mk4: the LEDGER (annotations · transcript · asks · log) rides the
// right column beside the work; THE DECK (agent cards) is the bottom
// band — vocodeck, full circle.
const dock = h("div", { class: "dock" });
const deckEl = h("div", { class: "deckrow" });
const gripDeck = h("div", { class: "grip h", role: "separator", tabindex: "0",
  "aria-label": "resize deck" });
const statusline = h("div", { class: "statusline" });
const body = h("div", { class: "deck-body" }, rail, gripRail, work, gripDock, dock);
app.append(presence, body, gripDeck, deckEl, statusline);

// ---- toasts (policy: errors persist w/ dismiss; successes fade) ---------------
/** @returns {HTMLElement} the toast node (P4: daemon alerts dedupe on it) */
function toast(msg, sticky = false) {
  const t = h("div", { class: "toast-msg" + (sticky ? " sticky" : "") }, msg);
  if (sticky) {
    const dismiss = h("button", { class: "toast-x", type: "button",
      title: "dismiss", "aria-label": "dismiss notification",
      onclick: (event) => {
        event.preventDefault();
        event.stopPropagation();
        t.remove();
      } }, "✕");
    t.append(dismiss);
  }
  document.body.append(t);
  if (!sticky) setTimeout(() => t.remove(), 4000);
  return t;
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

// ---- content cache (page_id -> {version, content}) --------------------------
const contentCache = new Map();
async function fetchContent(page) {
  // updated_ts covers legitimate same-rev refreshes while rev remains the
  // durable review revision. Snapshot epoch prevents page-id reuse after a
  // daemon restart from reviving content from the previous process.
  const version = `${store.snapshotEpoch}:${page.rev}:${page.updated_ts || 0}`;
  // Path-backed docs are intentionally read fresh by the server. The client
  // cannot distinguish them from virtual docs from metadata alone, so all
  // docs use read-through behavior (small cost, correct contract).
  const cacheable = page.type !== "doc";
  const hit = contentCache.get(page.page_id);
  if (cacheable && hit && hit.version === version) return hit.content;
  const resp = await fetch(`/v1/page/${page.page_id}`, {
    headers: { "x-voco-wb": (window.__VOCO__ || {}).wb || "" },
  });
  if (!resp.ok) throw new Error(`page ${page.page_id}: ${resp.status}`);
  const body = await resp.json();
  if (cacheable) {
    contentCache.delete(page.page_id); // refresh insertion order (tiny LRU)
    contentCache.set(page.page_id, { version, content: body.content });
    if (contentCache.size > 128)
      contentCache.delete(contentCache.keys().next().value);
  }
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

const stateOf = (s) => s.display_state || s.state || "idle";
// "listening" renders as "ready" (audit): the state means the agent is
// parked at its listen call — the word listening belongs to the mic.
const stateWord = (st) => (st === "listening" ? "ready" : st);

function dot(s) {
  const state = stateOf(s);
  return h("span", { class: "dot " + state, title: state });
}

// STABLE order (decision sweep): rows never jump on state changes —
// spatial memory wins; urgency speaks through the LEDs and words.
function agentsIn(ws) {
  return [...store.sessions.values()]
    .filter((s) => sessionInWs(s, ws))
    .sort((a, b) => a.name.localeCompare(b.name));
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

// ---- mic model (ADR-0003: selection is routing) ---------------------------------
// Clicking an AGENT (tree row, deck card) views AND talks — eye
// contact. The 🔒 lock pins the mic for split attention: while locked,
// agent clicks are view-only and only explicit movers (patch, ⌘K
// mic→, spoken switch) re-route. Mic follows people, never places:
// work-row/page browsing derives a view focus and never touches it.
let micLock = false;

function setMicLock(on) {
  micLock = on;
  renderRackPanel(); renderStrip(); renderRail();
}

/** View an agent's work WITHOUT moving the mic (derived focus, or any
 * agent click while the mic is locked). */
function focusAgent(s) {
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
  return ws;
}

/** Agent click: view + mic (selection is routing). While the mic is
 * locked, unforced clicks degrade to view-only; force = the explicit
 * movers (MIC patch, ⌘K mic→). */
async function selectAgent(s, { force = false } = {}) {
  focusAgent(s);
  if (micLock && !force) return; // locked: look, don't re-route
  try { await bus.command("switch_session", { name: s.name }); }
  catch (e) { toast("activate failed: " + errMsg(e), true); }
  // Review routing is NOT set here (decision sweep after ADR-0003):
  // the daemon ELECTS the primary and its election already prefers the
  // active (mic-holding) session when reachable — the old per-click
  // review.primary override pinned what should stay fluid and fought
  // that election. Explicit pinning returns as UI if real use asks.
}

// Voice/daemon-initiated mic moves (spoken switch, another client):
// selection FOLLOWS the mic unless locked — the model is symmetric.
let lastFollowedActive = null;
store.subscribe("sessions", () => {
  const active = store.activeSession;
  if (active && active !== lastFollowedActive) {
    lastFollowedActive = active;
    if (!micLock && store.selectedAgent !== active) {
      const s = store.sessions.get(active);
      if (s) focusAgent(s);
    }
  }
});

async function detachAgent(s) {
  // Undo-over-confirm (design policy): detach never touches the process
  // and the agent re-registers on its next call — no dialog earned.
  try {
    await bus.command("session.detach", { name: s.name });
    toast(`forgot ${s.name} — it reappears on its next call`);
  } catch (e) { toast("detach failed: " + errMsg(e), true); }
}

// ---- fleet tree (mk3 M2): groups → work rows → agents + pages -------------------
const PAGE_ICON = { screen: "▦", diff: "±", doc: "¶", terminal: "❯", html: "▣" };
// SVG twins of PAGE_ICON for DOM contexts (text contexts — the page
// bar provenance string, anchor labels — keep the unicode glyphs).
const PAGE_ICON_SVG = {
  screen: "screen", diff: "diff", doc: "doc", terminal: "term", html: "screen",
};

function groupKey(ws) { return ws.common_dir || ws.key; }

// Client-local expansion state: the selected work is always expanded;
// carets let the reader hold other rows open or fold groups away.
// Both persist across loads (mk3.1 batch #14).
const collapsedGroups = new Set(persisted("voco.grpFold", []));
const expandedWork = new Set(persisted("voco.workOpen", []));

function renderRail() {
  const keep = rail.scrollTop; // rebuilds must not move the reader
  rail.replaceChildren();
  rail.append(h("div", { class: "tree-head caps" }, "FLEET"));
  // Repo groups hold WORK ROWS (kind=workspace only); sessionspace
  // agents render as bare agent rows under SESSIONS (ADR-0001).
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
  for (const [gkey, g] of groups) {
    const groupWs = () =>
      g.works.find((w) => w.key === store.selectedWorkspace) || g.works[0];
    const folded = collapsedGroups.has(gkey);
    const toggle = () => {
      if (folded) collapsedGroups.delete(gkey); else collapsedGroups.add(gkey);
      persistSet("voco.grpFold", collapsedGroups);
      renderRail();
    };
    rail.append(h("div", { class: "grp" },
      h("span", { class: "tw", onclick: toggle }, folded ? "▸" : "▾"),
      h("span", { class: "grp-name", onclick: toggle }, g.name),
      h("span", { class: "grp-ops" },
        h("span", { class: "grp-op", title: "review a diff here — no agent needed",
          onclick: (e) => { e.stopPropagation(); openPickerFor(groupWs()); } },
          "+rev"),
        h("span", { class: "grp-op", title: "spawn an agent in a new worktree",
          onclick: (e) => { e.stopPropagation();
            openSpawn({ ...mctx(), rootHint: groupWs().root }); } },
          "+agt"))));
    if (folded) continue;
    // stable label order — rows never jump when agent states change
    const rows = g.works.map((ws) => ({ ws, agents: agentsIn(ws) }))
      .sort((a, b) => workLabel(a.ws).localeCompare(workLabel(b.ws)));
    for (const r of rows) rail.append(workRow(r.ws, r.agents));
  }
  // Agents outside any checkout (sessionspaces, pre-identity strays) —
  // membership by identity, so folded groups don't leak agents here.
  const stray = [...store.sessions.values()].filter((s) => !wsOf(s));
  if (stray.length) {
    rail.append(h("div", { class: "grp" },
      h("span", { class: "tw" }, "▾"),
      h("span", { class: "grp-name", title:
        "agents running outside any git checkout" }, "SESSIONS")));
    for (const s of stray) rail.append(agentRow(s, true));
  }
  if (!store.sessions.size && !store.workspaces.size)
    rail.append(h("div", { class: "empty-note" },
      "no agents — run voice_init (MCP) or `voco listen` in a repo"));
  rail.append(h("div", { class: "rail-action rail-foot",
    onclick: () => openConnect(mctx()) }, "connect →"));
  rail.scrollTop = keep;
}


function workRow(ws, agents) {
  const sel = store.selectedWorkspace === ws.key;
  const expanded = sel || expandedWork.has(ws.key);
  const meta = h("span", { class: "wr-meta" });
  const l = ws.links || {};
  const g = ws.git || null;
  const parts = [];
  if (l.issue) parts.push(linkChip("issue", l.issue));
  if (l.pr) parts.push(linkChip("pr", l.pr));
  const gitBits = [];
  if (g) {
    const changed = (g.staged || 0) + (g.unstaged || 0);
    if (changed) gitBits.push("±" + changed);
    if (g.untracked) gitBits.push("?" + g.untracked);
  }
  if (gitBits.length)
    parts.push(h("span", { title: gitTitle(g) }, gitBits.join(" ")));
  if (g && g.ahead)
    parts.push(h("span", { class: "g", title: gitTitle(g) }, "↑" + g.ahead));
  if (g && g.behind)
    parts.push(h("span", { class: "g", title: gitTitle(g) }, "↓" + g.behind));
  const flags = openFindingCount(ws);
  if (flags)
    parts.push(h("span", { class: "hot", title: "open annotations" },
      flags + "⚑"));
  const asksN = openAskCount(ws);
  if (asksN)
    parts.push(h("span", { class: "hot", title: "unanswered asks" },
      "?" + asksN));
  if (!agents.length && !parts.length)
    parts.push(h("span", {}, "no agent"));
  parts.forEach((p, i) => { if (i) meta.append(" · "); meta.append(p); });
  const caret = h("span", { class: "tw",
    title: expanded ? "collapse" : "expand",
    onclick: (e) => {
      e.stopPropagation();
      if (expandedWork.has(ws.key)) expandedWork.delete(ws.key);
      else expandedWork.add(ws.key);
      persistSet("voco.workOpen", expandedWork);
      renderRail();
    } }, expanded ? "▾" : "▸");
  const row = h("div", {
    class: "work-row" + (sel ? " sel" : "") + (agents.length ? "" : " parked"),
    onclick: () => selectWork(ws) },
    caret,
    h("span", { class: "glyph" }, ic("branch")),
    h("span", { class: "wr-label" }, workLabel(ws)),
    meta);
  if (!expanded) return row;
  const box = h("div", {}, row);
  for (const s of agents) box.append(agentRow(s));
  box.append(pagesTree(ws));
  return box;
}

const gitTitle = (g) => !g ? "" :
  `${g.staged} staged · ${g.unstaged} unstaged · ${g.untracked} untracked`
  + (g.ahead != null ? ` · ${g.ahead} ahead ${g.behind} behind upstream` : "");

/** Child row inside an expanded work row, or bare (SESSIONS group). */
function agentRow(s, bare = false) {
  const sel = store.selectedAgent === s.session_id;
  const row = h("div", {
    class: "agent-row" + (bare ? " bare" : "") + (sel ? " sel" : ""),
    title: micLock ? `view ${s.name} (mic locked)`
      : `talk to ${s.name} — selection is routing`,
    onclick: () => selectAgent(s) },
    // the module icon IS the state LED (one signal, not two glyphs);
    // the state word stays adjacent in ar-state per the design system
    ic("module", "ic mstate " + stateOf(s)),
    h("span", { class: "ar-name" }, s.name),
    store.speaking && store.speaking.who === s.name ? speakingEq() : "",
    h("span", { class: "ar-meta" },
      store.activeSession === s.session_id
        ? h("span", { class: "hot", title: "holds the mic" }, "MIC · ") : "",
      s.queued ? h("span", { class: "hot" }, "q" + s.queued + " · ") : "",
      h("span", { class: "ar-state " + stateOf(s) }, stateWord(stateOf(s)))),
    h("span", { class: "rail-x", title: "forget this session",
      onclick: (e) => { e.stopPropagation(); detachAgent(s); } }, "✕"));
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

/** Open findings / unanswered asks for a work row — SEPARATE badges
 * (⚑N annotations · ?N asks — mk3.1 batch #12). Unvisited rows fall
 * back to the snapshot's counts, so parked work stays visible. */
function openFindingCount(ws) {
  if (!ws) return 0;
  return store.findings.has(ws.key)
    ? store.findingsFor(ws.key).filter((f) => f.status === "open").length
    : ((ws.finding_counts && ws.finding_counts.open) || 0);
}
function openAskCount(ws) {
  if (!ws) return 0;
  return store.asks.has(ws.key)
    ? store.asksFor(ws.key).filter((a) => a.answer == null).length
    : (ws.open_asks || 0);
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

// mk3 decision (BUILD-CONSOLE.md): the diff file sub-tree left the tree —
// the diff view's own collapsed file index owns per-file navigation.
function pagesTree(ws) {
  const tree = h("div", { class: "pages" });
  tree.append(h("div", {
    class: "page-row" + (store.selectedPage == null ? " sel" : ""),
    onclick: (e) => { e.stopPropagation(); store.selectPage(null); } },
    h("span", { class: "picon" }, ic("overview")),
    h("span", { class: "page-title" }, "overview")));
  if (ws && ws.kind === "workspace")
    tree.append(h("div", {
      class: "page-row" + (store.selectedPage === "__files__" ? " sel" : ""),
      onclick: (e) => { e.stopPropagation(); store.selectPage("__files__"); } },
      h("span", { class: "picon" }, ic("files")),
      h("span", { class: "page-title" }, "files")));
  const pages = ws
    ? ws.pages.filter((p) => !p.closed)
      .sort((a, b) => (a.pinned ? 0 : 1) - (b.pinned ? 0 : 1)
        || a.page_id.localeCompare(b.page_id))
    : [];
  for (const p of pages) {
    const row = h("div", {
      class: "page-row" + (p.page_id === store.selectedPage ? " sel" : ""),
      onclick: (e) => { e.stopPropagation(); store.selectPage(p.page_id); } },
      h("span", { class: "picon" }, ic(PAGE_ICON_SVG[p.type] || "doc")),
      h("span", { class: "page-title" }, p.title),
      p.rev > 1 ? h("span", { class: "rev",
        title: `revision ${p.rev} — republished ${p.rev - 1}×` },
        "r" + p.rev) : "");
    if (!p.pinned)
      row.append(h("span", { class: "rail-x", title: "close page",
        onclick: (e) => { e.stopPropagation(); closePage(p); } }, "✕"));
    tree.append(row);
  }
  return tree;
}

// Fold state per diff page — survives re-renders so the reader keeps
// their place; reseeded when the page rev moves (new diff, new folds).
const foldCache = new Map();
/** @type {?{pageId:string, path?:string, text?:string, selector?:string}} */
let pendingReveal = null;

/** B1b deep links (da:…) from inside artifacts — everything rides the
 * existing reveal machinery. */
function routeDeepLink(target) {
  const ws = store.selectedWs();
  if (!ws) return;
  const m = /^da:([a-z]+)\/(.*)$/.exec(String(target));
  if (!m) return;
  const rest = decodeURIComponent(m[2]);
  const pages = ws.pages.filter((p) => !p.closed);
  if (m[1] === "diff") {
    const [file, line] = rest.split(":");
    const page = pages.find((p) => p.type === "diff");
    if (!page) return;
    pendingReveal = { pageId: page.page_id, path: file };
    store.selectPage(page.page_id);
    if (line) blinkRow({ file, side: "new", startLine: Number(line) });
  } else if (m[1] === "doc") {
    const [name, text] = rest.split(":");
    const page = pages.find((p) => p.type === "doc" && p.title === name)
      || pages.find((p) => p.type === "doc");
    if (!page) return;
    pendingReveal = { pageId: page.page_id, text: text || name };
    store.selectPage(page.page_id);
  } else if (m[1] === "section") {
    const page = pages.find((p) => p.title === rest);
    if (page) store.selectPage(page.page_id);
  } else if (m[1] === "file") {
    fstate(ws.key).path = rest;
    store.selectPage("__files__");
  }
}

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
  // the tab strip shows EVERY open page — new/republished/closed pages
  // must rebuild the canvas even when the selected page didn't change
  parts.push(pages.map((p) =>
    p.page_id + ":" + p.rev + ":" + (p.updated_ts || 0)).join(","));
  if (store.selectedPage === "__files__" && ws) {
    // the file browser is client-local state — agent churn must not
    // rebuild it (the filter box would lose focus mid-typing)
    parts.push("files", fstate(ws.key).path);
    return parts.join("|");
  }
  if (page) {
    parts.push(page.rev, page.updated_ts || 0, page.title);
    // findings shape the diff's marks/chips — only THIS page's matter
    if (page.type === "diff")
      parts.push(store.findingsFor(ws ? ws.key : "")
        .filter((f) => f.page_id === page.page_id)
        .map((f) => f.finding_id + f.status + ":" + f.rev).join(","));
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
  // An OPEN annotation editor owns the center: a live-tracked worktree
  // diff bumps rev every few seconds while the agent works, and a
  // rebuild would eat the reviewer's half-written draft (xai B1a B2).
  // lastWorkKey stays stale on purpose — the next event after the
  // editor closes (the finding.added itself, or the live-git tick)
  // triggers the deferred rebuild.
  if (!force && work.querySelector(".annot-editor")) return;
  lastWorkKey = key;
  currentDiffApi = null; // re-set by the diff branch when a diff shows
  work.replaceChildren();
  const agent = selectedAgent();
  const ws = store.selectedWs();
  if (!agent && !ws) {
    work.append(h("div", { class: "view" }, welcomeView()));
    return;
  }
  const pages = ws ? ws.pages.filter((p) => !p.closed) : [];
  const page = pages.find((p) => p.page_id === store.selectedPage);
  const isFiles = store.selectedPage === "__files__" && ws
    && ws.kind === "workspace";
  // mk3 M3: the tab strip — open pages as tabs (the tree's page rows,
  // mirrored; no new state). ✕ shows on hover/active only (mk3.1 #1).
  const tabbar = h("div", { class: "tabbar" });
  const sorted = [...pages].sort((a, b) =>
    (a.pinned ? 0 : 1) - (b.pinned ? 0 : 1)
    || a.page_id.localeCompare(b.page_id));
  for (const p of sorted) {
    const on = p.page_id === store.selectedPage;
    const t = h("div", { class: "tab" + (on ? " on" : ""),
      onclick: () => store.selectPage(p.page_id) },
      h("span", { class: "g" }, ic(PAGE_ICON_SVG[p.type] || "doc")),
      h("span", { class: "tab-title" }, p.title),
      p.rev > 1 ? h("span", { class: "g" }, "@r" + p.rev) : "");
    if (!p.pinned)
      t.append(h("span", { class: "tab-x", title: "close page",
        onclick: (e) => { e.stopPropagation(); closePage(p); } }, "✕"));
    tabbar.append(t);
  }
  if (ws && ws.kind === "workspace")
    tabbar.append(h("div", { class: "tab" + (isFiles ? " on" : ""),
      onclick: () => store.selectPage("__files__") },
      h("span", { class: "g" }, ic("files")),
      h("span", { class: "tab-title" }, "files")));
  if (ws)
    tabbar.append(h("div", { class: "tab plus", title: "open a review diff",
      onclick: () => openPickerFor(ws) }, "+"));
  // page bar: provenance · since-rev note · annotate hint · page actions
  const prov = h("span", { class: "pg-prov" },
    h("b", {}, ws ? (ws.repo || ws.name) + "/" + workLabel(ws)
      : (agent ? agent.name : "")),
    page
      ? h("span", {}, ` · ${PAGE_ICON[page.type] || "·"} ${page.type}`
        + ` · rev ${page.rev}`
        + (page.updated_ts ? " · pushed "
          + new Date(page.updated_ts * 1000).toTimeString().slice(0, 8) : "")
        + (page.call_name ? ` by ${page.call_name}` : ""))
      : h("span", {}, isFiles ? " · ▤ files" : " · overview"));
  if (page && page.rev > 1)
    prov.title = `republished ${page.rev - 1}× — annotations from older`
      + " revisions are marked stale, never dropped";
  const srnote = h("span", { class: "sr-note" });
  const actions = h("div", { class: "work-actions" });
  const canToggleAnnotations = !!ws && (!page
    || page.type === "doc" || page.type === "screen" || page.type === "diff"
    || isFiles);
  const annotationToggle = canToggleAnnotations
    ? h("button", {
      class: "whbtn annotate-toggle" + (annotationMode ? " on" : ""),
      type: "button",
      "aria-pressed": String(annotationMode),
      title: annotationMode
        ? "annotation mode on — click to return to normal select/copy"
        : "annotation mode off — normal select/copy",
      onclick: () => {
        annotationMode = !annotationMode;
        localStorage.setItem("voco.annotationMode", JSON.stringify(annotationMode));
        document.querySelectorAll(".annot-editor").forEach((n) => n.remove());
        renderWork(true);
      },
    }, annotationMode ? "annotate on" : "annotate off") : null;
  const hint = !annotationMode ? ""
    : page && (page.type === "doc" || page.type === "screen")
      ? "click block · select text"
      : page && page.type === "diff" ? "click a line"
        : isFiles ? "select code" : "";
  // export lives on the overview card + palette now (audit #1): it
  // writes interop FILES for external tools — agents already receive
  // annotations live, so it is not a step in the loop.
  work.append(tabbar,
    h("div", { class: "pgbar" }, prov, srnote,
      hint ? h("span", { class: "pg-hint" }, hint) : "",
      annotationToggle || "",
      actions));
  const view = h("div", { class: "view" });
  // Capture the key this view was RENDERED for: saving under a live
  // pageKey() re-keyed page A's offset onto page B when the selection
  // had already moved — "opens at the bottom" (B0-2). The listener is
  // also the ONLY writer; a pre-rebuild save is redundant with it.
  const renderedKey = pageKey();
  view.addEventListener("scroll",
    () => scrollMemo.set(renderedKey, view.scrollTop), { passive: true });
  work.append(view);
  if (isFiles) { renderFilesView(view, ws); return; }
  if (page) { renderPage(view, page, srnote, actions); return; }
  if (agent) { renderAgentCard(view, agent, ws); return; }
  if (ws) { renderWorkCard(view, ws); return; }
  view.classList.add("empty");
  view.append(h("div", { class: "empty-note" }, "no pages here yet"));
}

// ---- files (M4): tracked-file TREE + highlighted confined source ---------------
// Tree semantics ported from the reference explorer: directories are
// collapsible, single-child directory chains render compressed
// ("src/voco/server/"), files open the read-only source view. The
// filter keeps B1c's flat-match behavior (typing = flat hit list).
const filesState = new Map(); // wsKey -> {path, filter, list, truncated, open}
function fstate(wsKey) {
  if (!filesState.has(wsKey)) {
    if (filesState.size > 20) // bounded: drop the oldest browsed workspace
      filesState.delete(filesState.keys().next().value);
    filesState.set(wsKey, { path: null, filter: "", list: null, truncated: 0,
      open: new Set(persisted("voco.ftree." + wsKey, [])) });
  }
  return filesState.get(wsKey);
}

/** @typedef {{dirs: Map<string, FileNode>, files: {name:string, path:string}[]}} FileNode */
/** @param {string[]} paths @returns {FileNode} */
function buildFileTree(paths) {
  /** @type {FileNode} */
  const root = { dirs: new Map(), files: [] };
  for (const p of paths) {
    const parts = p.split("/");
    let node = root;
    for (let i = 0; i < parts.length - 1; i++) {
      const d = parts[i];
      if (!node.dirs.has(d)) node.dirs.set(d, { dirs: new Map(), files: [] });
      node = /** @type {FileNode} */ (node.dirs.get(d));
    }
    node.files.push({ name: parts[parts.length - 1], path: p });
  }
  return root;
}

const LANG_BY_EXT = { js: "javascript", mjs: "javascript", cjs: "javascript",
  jsx: "javascript", ts: "typescript", tsx: "typescript", py: "python",
  rb: "ruby", rs: "rust", go: "go", sh: "bash", bash: "bash", zsh: "bash",
  yml: "yaml", yaml: "yaml", md: "markdown", html: "xml", htm: "xml",
  xml: "xml", css: "css", scss: "scss", json: "json", toml: "ini",
  ini: "ini", sql: "sql", c: "c", h: "c", cpp: "cpp", java: "java",
  kt: "kotlin", swift: "swift", diff: "diff", patch: "diff" };

async function renderFilesView(view, ws) {
  const st = fstate(ws.key);
  if (st.path) return renderFileSource(view, ws, st);
  if (!st.list) {
    view.textContent = "…";
    try {
      const r = await bus.command("workspace.files", { workspace: ws.key });
      st.list = r.files || [];
      st.truncated = r.truncated || 0;
    } catch (e) {
      view.textContent = "files unavailable: " + errMsg(e);
      return;
    }
  }
  view.replaceChildren();
  const filter = /** @type {HTMLInputElement} */ (h("input", {
    class: "file-filter", type: "text",
    placeholder: `filter ${st.list.length} tracked files…` }));
  filter.value = st.filter;
  const listBox = h("div", { class: "file-list" });
  const openFile = (p) => { st.path = p; renderWork(true); };

  const renderFlat = (q) => {
    const hits = st.list.filter((f) => f.toLowerCase().includes(q));
    for (const f of hits.slice(0, 500))
      listBox.append(h("div", { class: "file-row",
        onclick: () => openFile(f) }, f));
    if (hits.length > 500)
      listBox.append(h("div", { class: "empty-note" },
        `+${hits.length - 500} more — narrow the filter`));
    if (!hits.length)
      listBox.append(h("div", { class: "empty-note" }, "no matches"));
  };

  const renderDir = (node, prefix, depth) => {
    const pad = (d) => "padding-left:" + (8 + d * 16) + "px";
    const dirs = [...node.dirs.entries()]
      .sort((a, b) => a[0].localeCompare(b[0]));
    for (let [name, child] of dirs) {
      // compress single-child chains: src/voco/server as one row
      let label = name;
      let path = prefix + name;
      while (child.files.length === 0 && child.dirs.size === 1) {
        const next = child.dirs.entries().next().value;
        if (!next) break;
        label += "/" + next[0];
        path += "/" + next[0];
        child = next[1];
      }
      const open = st.open.has(path);
      const kid = child;
      listBox.append(h("div", { class: "ftree-row ftree-dir",
        style: pad(depth),
        onclick: () => {
          if (open) st.open.delete(path); else st.open.add(path);
          persistSet("voco.ftree." + ws.key, st.open);
          renderList();
        } },
        h("span", { class: "tw" }, open ? "▾" : "▸"),
        h("span", {}, label + "/")));
      if (open) renderDir(kid, path + "/", depth + 1);
    }
    for (const f of node.files.sort((a, b) => a.name.localeCompare(b.name)))
      listBox.append(h("div", { class: "ftree-row ftree-file",
        style: pad(depth), onclick: () => openFile(f.path) },
        h("span", { class: "tw" }, ""),
        h("span", {}, f.name)));
  };

  const renderList = () => {
    listBox.replaceChildren();
    const q = st.filter.trim().toLowerCase();
    if (q) { renderFlat(q); return; }
    const tree = buildFileTree(st.list);
    // first visit: auto-open a lone root chain so the tree isn't a
    // single collapsed row
    const first = tree.dirs.entries().next().value;
    if (!st.open.size && tree.dirs.size === 1 && !tree.files.length && first) {
      let [path, child] = first;
      while (child.files.length === 0 && child.dirs.size === 1) {
        const next = child.dirs.entries().next().value;
        if (!next) break;
        path += "/" + next[0];
        child = next[1];
      }
      st.open.add(path);
    }
    renderDir(tree, "", 0);
  };
  filter.addEventListener("input", () => { st.filter = filter.value; renderList(); });
  renderList();
  view.append(filter, listBox);
  if (st.truncated)
    view.append(h("div", { class: "micro" },
      `${st.truncated} more file(s) beyond the listing cap`));
}

/** Absolute character offset of (node, nodeOffset) within root's text. */
function offsetIn(root, node, nodeOffset) {
  const r = document.createRange();
  r.selectNodeContents(root);
  try { r.setEnd(node, nodeOffset); } catch { return 0; }
  return r.toString().length;
}

async function renderFileSource(view, ws, st) {
  view.textContent = "…";
  const back = () => { st.path = null; renderWork(true); };
  try {
    const resp = await fetch(
      `/v1/file?workspace=${encodeURIComponent(ws.key)}`
      + `&path=${encodeURIComponent(st.path)}`,
      { headers: { "x-voco-wb": (window.__VOCO__ || {}).wb || "" } });
    if (!resp.ok) throw new Error(`${resp.status}: ${await resp.text()}`);
    const body = await resp.json();
    const ext = (st.path.match(/\.([a-z0-9]+)$/i) || [""])[1] || "";
    const code = h("code", { class: ext ? "language-" + ext : "" },
      body.content);
    const pre = h("pre", { class: "file-src" }, code);
    const head = h("div", { class: "file-head" },
      h("button", { class: "whbtn", onclick: back }, "← files"),
      h("span", { class: "mono" }, st.path),
      h("span", { class: "micro" }, annotationMode
        ? "select code to annotate" : "normal select/copy mode"));
    view.replaceChildren(head, pre);
    highlightCode(/** @type {HTMLElement} */ (code),
      LANG_BY_EXT[ext.toLowerCase()] || "");
    // A3: select code → file finding ({kind:"file"} anchor, page-less;
    // reference note-bar concept on voco's finding seam)
    pre.addEventListener("mouseup", (e) => {
      if (!annotationMode) return;
      if (/** @type {Element} */ (e.target).closest(".annot-editor")) return;
      const sel = window.getSelection();
      if (!sel || sel.isCollapsed || !sel.rangeCount) return;
      const range = sel.getRangeAt(0);
      if (!code.contains(range.startContainer)
        || !code.contains(range.endContainer)) return;
      const exact = sel.toString();
      if (!exact.trim()) return;
      const full = code.textContent || "";
      const start = offsetIn(code, range.startContainer, range.startOffset);
      const end = offsetIn(code, range.endContainer, range.endOffset);
      openFileEditor(view, head, {
        kind: "file", file: st.path, exact,
        prefix: full.slice(Math.max(0, start - 40), start),
        suffix: full.slice(end, end + 40),
        startLine: full.slice(0, start).split("\n").length,
        endLine: full.slice(0, end).split("\n").length,
      });
    });
  } catch (e) {
    view.replaceChildren(
      h("div", { class: "file-head" },
        h("button", { class: "whbtn", onclick: back }, "← files")),
      h("div", { class: "empty-note" }, "could not read: " + errMsg(e)));
  }
}

function openFileEditor(view, afterEl, anchor) {
  view.querySelectorAll(".annot-editor").forEach((n) => n.remove());
  let kind = "concern";
  const short = anchor.file.split("/").pop();
  const lines = anchor.startLine === anchor.endLine
    ? "L" + anchor.startLine : `L${anchor.startLine}–L${anchor.endLine}`;
  const excerpt = anchor.exact.length > 60
    ? anchor.exact.slice(0, 57) + "…" : anchor.exact;
  const ta = /** @type {HTMLTextAreaElement} */ (h("textarea", {
    placeholder: "note or question about this code…" }));
  const pills = h("div", { class: "finding-controls" });
  const pillEls = [];
  for (const k of ["concern", "question", "nit"]) {
    const p = h("button", { class: "fpill" + (k === kind ? " active" : ""),
      onclick: () => {
        kind = k;
        for (const q of pillEls) q.classList.toggle("active", q.textContent === k);
      } }, k);
    pillEls.push(p);
    pills.append(p);
  }
  const blocking = /** @type {HTMLInputElement} */ (h("input", { type: "checkbox" }));
  pills.append(h("label", { class: "fblock" }, blocking, "blocking"));
  const box = h("div", { class: "doc-editor annot-editor" },
    h("div", { class: "editor-target" }, `${short} ${lines}: “${excerpt}”`),
    ta, pills,
    h("div", { class: "editor-actions" },
      h("button", { class: "tbtn primary", onclick: commit }, "add annotation"),
      h("button", { class: "tbtn",
        title: "ask the work's agents about this selection (audit #3)",
        onclick: () => {
          const text = ta.value.trim();
          if (!text) { ta.focus(); return; }
          askWithContext(text, anchor);
          box.remove();
        } }, "ask"),
      h("button", { class: "tbtn", onclick: () => box.remove() }, "cancel")));
  async function commit() {
    const text = ta.value.trim();
    if (!text) { ta.focus(); return; }
    try {
      await bus.command("finding.add", {
        workspace: store.selectedWorkspace, page_id: null,
        anchor, text, kind, blocking: blocking.checked,
      });
      box.remove();
      const s = window.getSelection();
      if (s && s.removeAllRanges) s.removeAllRanges();
    } catch (e) { toast("annotation failed: " + errMsg(e), true); }
  }
  ta.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); commit(); }
    if (e.key === "Escape") { e.preventDefault(); box.remove(); }
  });
  afterEl.after(box);
  ta.focus();
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
    kv("state", stateWord(stateOf(s))),
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
  card.append(linkEditor(ws));
  card.append(h("div", { class: "card-actions" },
    h("button", { class: "whbtn",
      title: "write diff-annotate-compatible review files for external"
        + " tools — agents already receive annotations live",
      onclick: () => exportReview() }, "export review file")));
  card.append(h("div", { class: "empty-note" }, agents.length
    ? "select a page in the rail — or click an agent to talk to it"
    : "review-only: no agent attached — diffs and annotations still work"));
  view.append(card);
}

/** A1: attach a PR / issue to this work. Manual always works (paste a
 * number or GitHub URL; Enter attaches); ✕ clears. gh detection stays
 * lazy and silent, and a manual set always wins over it (daemon rule).
 * The workspace.updated event re-renders chips everywhere. */
function parseLinkInput(raw) {
  const s = raw.trim();
  if (!s) return null;
  const mUrl = /^https?:\/\/\S*\/(?:pull|issues)\/(\d+)/.exec(s);
  if (mUrl) return { number: Number(mUrl[1]), url: s };
  const mNum = /^#?(\d+)$/.exec(s);
  if (mNum) return { number: Number(mNum[1]) };
  return null;
}

function linkEditor(ws) {
  const box = h("div", { class: "link-editor" });
  const row = (kind, label) => {
    const l = (ws.links || {})[kind];
    const line = h("div", { class: "le-row" },
      h("span", { class: "le-k" }, label));
    if (l) {
      line.append(linkChip(kind, l),
        h("span", { class: "le-x", title: "detach " + label,
          onclick: async () => {
            try {
              await bus.command("workspace.link",
                { workspace: ws.key, [kind]: null });
            } catch (e) { toast("detach failed: " + errMsg(e), true); }
          } }, "✕"));
      return line;
    }
    const input = /** @type {HTMLInputElement} */ (h("input", {
      class: "le-input", type: "text",
      placeholder: `#123 or ${label} URL — Enter attaches` }));
    input.addEventListener("keydown", async (e) => {
      if (e.key !== "Enter") return;
      const parsed = parseLinkInput(input.value);
      if (!parsed) { toast("need a number or a GitHub URL", true); return; }
      try {
        await bus.command("workspace.link",
          { workspace: ws.key, [kind]: parsed });
      } catch (err) { toast("attach failed: " + errMsg(err), true); }
    });
    line.append(input);
    return line;
  };
  box.append(row("pr", "pr"), row("issue", "issue"));
  return box;
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

let htmlCleanup = /** @type {?() => void} */ (null);

async function renderPage(view, page, srnote, actions) {
  const seq = ++renderSeq;
  const stale = () => seq !== renderSeq || !view.isConnected;
  if (htmlCleanup) { htmlCleanup(); htmlCleanup = null; } // message listener
  if (page.type === "html") {
    view.textContent = "…";
    try {
      const c = await fetchContent(page);
      if (stale()) return;
      let reveal = null;
      if (pendingReveal && pendingReveal.pageId === page.page_id
          && pendingReveal.selector) {
        reveal = pendingReveal.selector;
        pendingReveal = null;
      }
      htmlCleanup = renderHtmlView(view, c, {
        title: page.title,
        wb: (window.__VOCO__ || {}).wb || "",
        reveal,
        onAnnotate: (anchor, text, kind, blocking) =>
          addFinding(page, anchor, text, kind, blocking),
        onNav: routeDeepLink,
      });
    } catch (e) { if (!stale()) view.textContent = "could not load: " + errMsg(e); }
    return;
  }
  if (page.type === "screen" || page.type === "doc") {
    view.textContent = "…";
    try {
      const c = await fetchContent(page);
      if (stale()) return;
      // M4 FIX: every markdown-rendered page is an annotation surface —
      // agent-pushed SCREENS included. Screens used to render through
      // plain renderMarkdown with no annotation wiring, which is why
      // "click a block" did nothing on them. Select a passage or click
      // a block; text-range anchors re-anchor after edits; the server
      // accepts findings on any page type (finding.add is untyped).
      let reveal = null;
      if (pendingReveal && pendingReveal.pageId === page.page_id
          && pendingReveal.text) {
        reveal = pendingReveal.text;
        pendingReveal = null;
      }
      await renderDocView(view, c.markdown || "", {
        title: page.title,
        readOnly: !annotationMode
          || !!(c.params && c.params.annotatable === false),
        reveal,
        onAnnotate: (anchor, text, kind, blocking) =>
          addFinding(page, anchor, text, kind, blocking),
      });
      if (!stale() && !reveal) restoreScroll(view);
    } catch (e) { if (!stale()) view.textContent = "could not load: " + errMsg(e); }
    return;
  }
  if (page.type === "diff") {
    view.textContent = "…";
    try {
      const c = await fetchContent(page);
      if (stale()) return;
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
      // #15: reviewed marks are client-local per page@rev (a new rev is
      // a new review); #10: rows highlight lazily per opened file.
      const revKey = `voco.reviewed.${page.page_id}@${page.rev}`;
      const reviewed = new Set(persisted(revKey, []));
      const api = renderDiff(view, c, {
        rev: page.rev,
        annotationEnabled: annotationMode,
        findings,
        fold: fc.fold,
        reveal,
        scrollTo: (el) => scrollViewTo(el),
        onAnnotate: (anchor, text, kind, blocking) =>
          addFinding(page, anchor, text, kind, blocking),
        onAsk: (anchor, text) =>
          askWithContext(text, { kind: "diff", ...anchor }),
        onFoldChange: () => syncExpand(),
        highlight: (codeEl, path) => {
          const ext = (path.match(/\.([a-z0-9]+)$/i) || [""])[1] || "";
          return highlightCode(codeEl, LANG_BY_EXT[ext.toLowerCase()] || "");
        },
        reviewed,
        onReviewToggle: (path, on) => {
          if (on) reviewed.add(path); else reviewed.delete(path);
          persistSet(revKey, reviewed);
        },
      });
      currentDiffApi = api;
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
      const c = await fetchContent(page);
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
// The showing diff's api (j/k navigation); null on any other view.
let currentDiffApi = /** @type {?{nextChange:()=>void, prevChange:()=>void}} */ (null);

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

// ---- console (mk3 M6): annotations table · transcript · asks · log --------------
let dockTab = "annotations";
// A5: an agent answered while the asks tab wasn't showing — pulse it.
let askPulse = false;
// #14: console scroll survives tab/scope switches.
const dockScrollMemo = new Map();

function setDockTab(name) {
  dockTab = name;
  if (name === "asks") askPulse = false;
  renderDock();
}

/** Human-readable anchor location for the table's ANCHOR column. */
function anchorLabel(f) {
  const a = f.anchor || {};
  const ws = store.selectedWs();
  const p = ws && ws.pages.find((x) => x.page_id === f.page_id);
  const icon = p ? (PAGE_ICON[p.type] || "·") : "·";
  if (a.kind === "file" && a.file)
    return `▤ ${a.file}` + (a.startLine != null ? ":L" + a.startLine : "");
  if (a.file)
    return `± ${a.file}` + (a.startLine != null ? ":" + a.startLine : "");
  if (a.kind === "element" && a.selector)
    return `${icon} ${p ? p.title : ""} · ${a.selector}`;
  if (a.exact) {
    const q = a.exact.length > 26 ? a.exact.slice(0, 23) + "…" : a.exact;
    return `${icon} ${p ? p.title : ""} · “${q}”`;
  }
  return p ? `${icon} ${p.title}` : "";
}

/** #3: an ask that carries WHAT you're pointing at. The context rides
 * ask.create verbatim and reaches the agent with the question. */
async function askWithContext(text, context) {
  try {
    await bus.command("ask.create",
      { workspace: store.selectedWorkspace, text, context });
    setDockTab("asks");
  } catch (e) { toast("ask failed: " + errMsg(e), true); }
}

async function withdrawFinding(wsKey, f) {
  try {
    await bus.command("finding.withdraw",
      { workspace: wsKey, finding_id: f.finding_id });
    // Undo-over-confirm: withdraw is reversible, so no dialog —
    // the toast carries the way back (re-open via finding.status).
    toastUndo("annotation withdrawn —", async () => {
      try {
        await bus.command("finding.status",
          { workspace: wsKey, finding_id: f.finding_id, status: "open" });
      } catch (e) { toast("undo failed: " + errMsg(e), true); }
    });
  } catch (e) { toast("withdraw failed: " + errMsg(e), true); }
}

function annotationsEmptyText(ws) {
  // page-type-aware (the "click a diff line" hint on a doc page was a
  // reported wart) — say what THIS page annotates by.
  const page = ws && ws.pages.find(
    (p) => p.page_id === store.selectedPage && !p.closed);
  if (page && (page.type === "doc" || page.type === "screen"))
    return "no annotations — click a block or select text to flag one";
  if (page && page.type === "html")
    return "no annotations — toggle annotate, then click an element";
  if (page && page.type === "diff")
    return "no annotations — click a diff line to flag one";
  return (ws && ws.pages.some((p) => p.type === "diff" && !p.closed))
    ? "no annotations — click a diff line to flag one"
    : "no annotations — open a diff or a doc, then flag what you see";
}

function renderDock() {
  dock.replaceChildren();
  const wsKey = store.selectedWorkspace || "";
  const ws = store.selectedWs();
  const agent = selectedAgent();
  const findings = store.findingsFor(wsKey);
  const asks = store.asksFor(wsKey);
  const openF = findings.filter((f) => f.status === "open");
  const blockingN = openF.filter((f) => f.blocking).length;
  const openAsks = asks.filter((a) => a.answer == null).length;
  const memoKey = dockTab + ":" + wsKey;
  const keep = dockScrollMemo.get(memoKey) ?? 0;

  // counts render as step lights (index7 ledger form) — 4 cells, lit up
  // to the count; the exact number stays in the tooltip and the footer
  const tab = (name, count, hot) => {
    const t = h("div",
      { class: "ctab" + (dockTab === name ? " on" : "")
        + (name === "asks" && askPulse ? " pulse" : ""),
        role: "tab", "aria-selected": String(dockTab === name),
        title: count != null && count > 0 ? count + " open" : "",
        onclick: () => setDockTab(name) },
      name);
    if (count != null && count > 0) {
      const lights = h("span", { class: "lights" + (hot ? " hot" : "") });
      for (let i = 0; i < 4; i++)
        lights.append(h("i", { class: i < Math.min(count, 4) ? "on" : "" }));
      t.append(lights);
    }
    return t;
  };
  const scopeTxt = agent
    ? agent.name + (ws ? " · " + workLabel(ws) : "")
    : ws ? (ws.repo || ws.name) + "/" + workLabel(ws) : "nothing selected";
  dock.append(h("div", { class: "ctabs", role: "tablist" },
    tab("annotations", openF.length, false),
    tab("transcript", null, false),
    tab("asks", openAsks, true), // amber when a question is outstanding
    tab("log", null, false)));
  const body = h("div", { class: "cbody" });
  body.addEventListener("scroll",
    () => dockScrollMemo.set(memoKey, body.scrollTop), { passive: true });
  dock.append(body);
  // footer pinned under the scroll body (mk4 ledger)
  const foot = h("div", { class: "cfoot" },
    h("span", { title:
      "open annotations and asks reach this work's agents automatically"
      + " — no send step" },
      h("b", {}, openF.length + " open"),
      blockingN ? ` · ${blockingN} blocking` : ""),
    h("span", { class: "cfoot-scope" }, "scope: " + scopeTxt));
  dock.append(foot);

  if (dockTab === "transcript") {
    if (agent) {
      // split state (mic locked elsewhere): say whose ears your voice
      // actually reaches while this transcript is showing
      const holder = store.activeSession
        && store.sessions.get(store.activeSession);
      if (holder && holder.session_id !== agent.session_id)
        body.append(h("div", { class: "t-micnote" },
          "viewing " + agent.name + " · ",
          h("span", { class: "amber" }, "mic → " + holder.name)));
      renderTranscript(body, store.transcriptFor(agent.session_id),
        { agentName: agent.name, speaking: store.speaking });
      loadTranscript(agent); // refetch if stale
    } else {
      const n = ws ? agentsIn(ws).length : 0;
      body.append(h("div", { class: "empty-note" }, n
        ? "click an agent in the tree to focus its conversation"
        : "no agent attached here — transcripts appear when one connects"));
    }
    body.scrollTop = keep;
    return;
  }

  if (dockTab === "asks") {
    renderAsksTab(body, ws, wsKey, asks);
    return;
  }

  if (dockTab === "log") {
    for (const e of eventLog) {
      let pl = "";
      try { pl = JSON.stringify(e.payload || {}); } catch { pl = ""; }
      if (pl.length > 140) pl = pl.slice(0, 140) + "…";
      body.append(h("div", { class: "logln" },
        h("span", { class: "lg-ts" },
          new Date(e.ts * 1000).toTimeString().slice(0, 8)),
        h("span", { class: "lg-seq" }, String(e.seq)),
        h("span", { class: "lg-ty" }, e.type),
        h("span", { class: "lg-pl" }, pl)));
    }
    if (!eventLog.length)
      body.append(h("div", { class: "empty-note" }, "no events yet"));
    body.scrollTop = body.scrollHeight; // a log tails
    return;
  }

  // annotations: mk4 ledger rows (tall panel beside the work) -------------------
  if (!findings.length) {
    body.append(h("div", { class: "empty-note" }, annotationsEmptyText(ws)));
    body.scrollTop = keep;
    return;
  }
  const kindCls = { concern: "k-c", question: "k-q", nit: "k-n" };
  for (const f of findings) {
    const done = f.status !== "open";
    const findingPage = ws && ws.pages.find((p) => p.page_id === f.page_id);
    const staleFinding = !!findingPage && f.rev !== findingPage.rev;
    const textBox = h("div", { class: "ftext" }, f.text || "");
    const item = h("div", { class: "fitem" + (done ? " done" : ""),
      onclick: (e) => {
        // an in-place edit owns the row — clicks in it never reveal
        if (/** @type {Element} */ (e.target)
          .closest(".edit-ta, .editor-actions, .fops")) return;
        revealFinding(f);
      } },
      h("div", { class: "fh" },
        h("span", { class: kindCls[f.kind] || "k-n" }, f.kind),
        f.blocking ? h("span", { class: "blk" }, "⚑ blocking") : "",
        staleFinding ? h("span", {
          class: "fstate stale",
          title: `annotation is from r${f.rev}; page is now r${findingPage.rev}`,
        }, `stale r${f.rev}→r${findingPage.rev}`) : "",
        done ? h("span", { class: "fstate s-" + f.status }, f.status) : "",
        h("span", { class: "fops" }, ...(f.status === "open" ? [
          h("span", { title: "edit",
            onclick: (e) => { e.stopPropagation();
              editFindingInline(textBox, wsKey, f); } }, "✎"), " ",
          h("span", { title: "withdraw (undoable)",
            onclick: (e) => { e.stopPropagation();
              withdrawFinding(wsKey, f); } }, "✕")] : []))),
      textBox);
    if (f.answer)
      item.append(h("div", { class: "freply" },
        h("b", {}, "agent"), " — " + f.answer));
    if (f.note)
      item.append(h("div", { class: "freply" }, f.note));
    if (f.commit) // audit #4: the fixing commit, when an agent stamped it
      item.append(h("div", { class: "freply" },
        "✓ fixed in " + String(f.commit).slice(0, 10)));
    item.append(h("div", { class: "floc" }, anchorLabel(f)));
    body.append(item);
  }
  body.scrollTop = keep;
}

/** #9: edit an annotation's text in place (finding.update exists
 * server-side; the finding.updated event repaints the table). */
function editFindingInline(textCell, wsKey, f) {
  textCell.replaceChildren();
  const ta = /** @type {HTMLTextAreaElement} */ (h("textarea", { class: "edit-ta" }));
  ta.value = f.text || "";
  const save = async () => {
    const text = ta.value.trim();
    if (!text) { ta.focus(); return; }
    try {
      await bus.command("finding.update",
        { workspace: wsKey, finding_id: f.finding_id, text });
    } catch (e) { toast("edit failed: " + errMsg(e), true); }
  };
  ta.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); save(); }
    if (e.key === "Escape") { e.preventDefault(); renderDock(); }
  });
  textCell.append(ta, h("div", { class: "editor-actions" },
    h("button", { class: "tbtn primary", onclick: save }, "save"),
    h("button", { class: "tbtn", onclick: () => renderDock() }, "cancel")));
  ta.focus();
}

/** Asks: questions YOU send the work's agents (ask.create); agents
 * answer through the bridge and ask.answered lands here. */
function renderAsksTab(body, ws, wsKey, asks) {
  if (!ws || ws.kind !== "workspace") {
    body.append(h("div", { class: "empty-note" },
      "asks are per work — select a work row first"));
    return;
  }
  const input = /** @type {HTMLInputElement} */ (h("input", {
    class: "ask-input", type: "text",
    placeholder: `ask ${agentsIn(ws).map((a) => a.name).join(", ") || "the agents here"}…` }));
  input.addEventListener("keydown", async (e) => {
    if (e.key !== "Enter") return;
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    try { await bus.command("ask.create", { workspace: wsKey, text }); }
    catch (err) { toast("ask failed: " + errMsg(err), true); }
  });
  body.append(h("div", { class: "ask-composer" },
    h("span", { class: "cmd-gt" }, "?"), input));
  if (!asks.length) {
    body.append(h("div", { class: "empty-note" },
      "no asks — questions you send the work's agents land here, with their answers"));
    return;
  }
  for (const a of asks) {
    const open = a.answer == null;
    const when = a.created_ts
      ? new Date(a.created_ts * 1000).toTimeString().slice(0, 5) : "";
    const row = h("div", { class: "ask" + (open ? " waiting" : "") },
      h("div", { class: "ask-head" },
        h("b", {}, "you"), h("span", { class: "lg-ts" }, when),
        open ? h("span", { class: "hot" }, "unanswered") : ""),
      h("div", { class: "ask-text" }, a.text || ""));
    if (a.answer) {
      const ans = h("div", { class: "ask-answer" });
      renderMarkdown(ans, a.answer);
      row.append(ans);
    }
    body.append(row);
  }
  body.scrollTop = body.scrollHeight; // a thread reads newest-last
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
  // File findings are page-less: open the files view at that file.
  if (a.kind === "file" && a.file) {
    const ws = store.selectedWs();
    if (ws) { fstate(ws.key).path = a.file; store.selectPage("__files__"); }
    return;
  }
  // Diff anchors open the fold + blink the row; text anchors (docs)
  // flash the passage — both ride pendingReveal through the async
  // render (B1a: one reveal path for every surface).
  if (a.kind === "element" && a.selector) {
    pendingReveal = { pageId: f.page_id, selector: a.selector };
    store.selectPage(f.page_id);
    return;
  }
  if (a.kind === "text" || (a.exact && !a.file)) {
    pendingReveal = { pageId: f.page_id, text: a.exact };
    store.selectPage(f.page_id);
    return;
  }
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
  if (!wsKey || store.loadedFindingWorkspaces.has(wsKey)) return;
  try {
    const r = await bus.command("finding.list", { workspace: wsKey });
    store.setFindingSnapshot(wsKey, r.findings || []);
  } catch (e) { /* not connected yet; a later event fills it */ }
}

async function loadAsks(wsKey) {
  if (!wsKey || store.loadedAskWorkspaces.has(wsKey)) return;
  try {
    const r = await bus.command("ask.list", { workspace: wsKey });
    store.setAskSnapshot(wsKey, r.asks || []);
  } catch (e) { /* not connected yet; a later event fills it */ }
}

// ---- "full": the transcript IS the expansion ----------------------------------
function jumpToTranscript(target) {
  setDockTab("transcript");
  requestAnimationFrame(() => flashEntry(dock, target));
}

// ---- command bar + channel rack ---------------------------------------------
function renderStrip() {
  renderPresence(presence, store, {
    onSettings: () => openSettings(mctx()),
  });
}

function renderRackPanel() {
  renderRack(deckEl, store, {
    command: (cmd, payload) => bus.command(cmd, payload),
    selectAgent,
    focusAgent,
    stateOf,
    micLocked: () => micLock,
    onToggleLock: () => setMicLock(!micLock),
    onFull: jumpToTranscript,
    toast,
  });
}

// ---- status line: ambient truth (mk3 M1) --------------------------------------
// The clock is one persistent node ticked by an interval — never a
// full status re-render per second.
const statusClock = h("span", { class: "lcd dim" },
  new Date().toTimeString().slice(0, 8));
// The VU is likewise one persistent node: mic.level events drive its
// segments out-of-band ("miclevel" notify), never a status re-render.
const statusVu = h("span", { class: "vu", title: "mic input level" });
for (let i = 0; i < 22; i++) statusVu.append(h("i"));
let vuPeak = 0;
function setVuLevel(level) {
  const segs = statusVu.children, n = segs.length;
  const lit = Math.round(level * n);
  // the trailing zero is the daemon's last word at silence — clear the
  // peak with it, or it holds forever (no further events decay it)
  vuPeak = level === 0 ? 0 : Math.max(vuPeak - 0.6, lit);
  for (let i = 0; i < n; i++) {
    segs[i].className = i < lit ? ("lit" + (i > n * 0.75 ? " hi" : ""))
      : (i === Math.round(vuPeak) && vuPeak > 0 ? "peak" : "");
  }
}
setInterval(() => {
  statusClock.textContent = new Date().toTimeString().slice(0, 8);
}, 1000);

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
  // segments double as shortcuts (mk3.1 batch #13): mic → view the
  // holder's work; ann count → open the console's annotations tab
  // LCD language (index7): LED + host well, MIC routing well, live VU.
  statusline.replaceChildren(
    h("span", { class: "cmd-led " + (store.connected ? "on" : "off") }),
    h("span", { class: "lcd" + (store.connected ? " grn" : " dim") },
      store.connected ? location.host : "reconnecting…"),
    h("span", { class: "lcd status-mic sc" + (active ? "" : " dim"),
      title: active ? "view " + active.name + "'s work" : "",
      onclick: () => { if (active) focusAgent(active); } },
      active ? "MIC → " + active.name.toUpperCase() : "MIC → —"),
    statusVu,
    h("span", {}, [mic.attention, mic.duplex].filter(Boolean).join(" · ") || "headless"),
    h("span", { class: "spacer" }),
    h("span", {},
      counts.blocked ? h("span", { class: "bad" }, counts.blocked + " blocked · ") : "",
      h("span", {}, h("b", {}, String(counts.working)), " working · ",
        h("b", {}, String(counts.listening)), " ready")),
    openCount ? h("span", { class: "amber sc", title: "open the annotations tab",
      onclick: () => setDockTab("annotations") }, openCount + " ann") : "",
    statusClock);
}

// ---- disconnected: a designed state -------------------------------------------
function renderConn() {
  body.classList.toggle("offline", !store.connected);
  dock.classList.toggle("offline", !store.connected);
  deckEl.classList.toggle("offline", !store.connected);
  if (!store.connected) {
    // no daemon = no signal: dark meters, never a frozen last level
    store.micLevel = 0;
    setMicLevel(0);
    setVuLevel(0);
  }
  renderStrip(); renderRackPanel(); renderStatus();
  if (store.connected) {
    loadFindings(store.selectedWorkspace || "");
    loadAsks(store.selectedWorkspace || "");
    const agent = selectedAgent();
    if (agent) loadTranscript(agent);
  }
}

// ---- keyboard floor -------------------------------------------------------------
/** A2: the minimal palette's items — navigation + mic, rebuilt fresh
 * from the store on every open. */
function paletteItems() {
  const items = [];
  for (const ws of store.workspaces.values()) {
    if (ws.kind !== "workspace") continue;
    const wl = (ws.repo || ws.name) + "/" + workLabel(ws);
    items.push({ label: "go: " + wl, hint: "work",
      run: () => selectWork(ws) });
    for (const p of ws.pages.filter((x) => !x.closed))
      items.push({ label: `open: ${workLabel(ws)} · ${p.title}`, hint: p.type,
        run: () => { selectWork(ws); store.selectPage(p.page_id); } });
    items.push({ label: `open: ${workLabel(ws)} · files`, hint: "files",
      run: () => { selectWork(ws); store.selectPage("__files__"); } });
  }
  for (const s of store.sessions.values()) {
    items.push({ label: "view: " + s.name, hint: stateWord(stateOf(s)),
      run: () => focusAgent(s) });
    items.push({ label: "mic → " + s.name, hint: "patch",
      run: () => selectAgent(s, { force: true }) });
  }
  for (const t of ["annotations", "transcript", "asks", "log"])
    items.push({ label: "console: " + t, hint: "tab",
      run: () => setDockTab(t) });
  items.push({ label: "export review", hint: "action",
    run: () => exportReview() });
  return items;
}

const typing = () => {
  const a = document.activeElement;
  return !!a && (a.tagName === "INPUT" || a.tagName === "TEXTAREA");
};

// #7: Space is hold-PTT while attention is ptt_only (never while typing).
let spaceHeld = false;
document.addEventListener("keyup", (e) => {
  if (e.code === "Space" && spaceHeld) {
    spaceHeld = false;
    bus.command("ptt.release").catch(() => {});
  }
});

document.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
    e.preventDefault();
    if (paletteOpen()) closePalette();
    else openPalette({ items: paletteItems() });
    return;
  }
  if (e.code === "Space" && !typing() && !paletteOpen()
      && store.mic && store.mic.attention === "ptt_only") {
    e.preventDefault(); // Space is the mic here, not page-scroll
    if (!e.repeat && !spaceHeld) {
      spaceHeld = true;
      bus.command("ptt.press").catch(() => { spaceHeld = false; });
    }
    return;
  }
  if (e.key === "Escape") {
    document.querySelectorAll(".annot-editor").forEach((n) => n.remove());
    // the editor's range highlight dies with it (self-review: Esc used
    // to strand .selected rows)
    document.querySelectorAll(".drow.selected")
      .forEach((n) => n.classList.remove("selected"));
    return;
  }
  // #15: j/k walk a diff's change blocks (never while typing)
  if (!typing() && currentDiffApi && (e.key === "j" || e.key === "k")) {
    e.preventDefault();
    if (e.key === "j") currentDiffApi.nextChange(); else currentDiffApi.prevChange();
  }
});

// ---- panel resize (persisted) ----------------------------------------------------
function grip(el, cssVar, min, max, invert, storeKey, opts = {}) {
  const target = opts.target || body;
  const axis = opts.vertical ? "clientY" : "clientX";
  const saved = localStorage.getItem(storeKey);
  if (saved) target.style.setProperty(cssVar, saved + "px");
  el.addEventListener("pointerdown", (down) => {
    down.preventDefault();
    el.setPointerCapture(down.pointerId);
    el.classList.add("dragging");
    const start = down[axis];
    const startW = parseFloat(getComputedStyle(target).getPropertyValue(cssVar));
    const move = (mv) => {
      const d = invert ? start - mv[axis] : mv[axis] - start;
      const w = Math.min(max, Math.max(min, startW + d));
      target.style.setProperty(cssVar, w + "px");
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
grip(gripDock, "--dockw", 260, 520, true, "voco.dockw");
// deck height: the grip rides ABOVE the deck, so dragging up grows it
grip(gripDeck, "--deckh", 144, 420, true, "voco.deckh",
  { vertical: true, target: deckEl });

// ---- wire subscriptions -----------------------------------------------------
store.subscribe("workspaces", () => { renderRail(); renderWork(); renderDock(); });
store.subscribe("sessions", () => { renderRail(); renderWork(); renderStrip();
  renderRackPanel(); renderStatus(); });
store.subscribe("selection", () => {
  renderRail(); renderWork(); renderDock();
  renderRackPanel(); // the deck marks the VIEWED agent's card (steel)
  loadFindings(store.selectedWorkspace || "");
  loadAsks(store.selectedWorkspace || "");
});
store.subscribe("findings", () => { renderDock(); renderWork(); renderRail(); renderStatus(); });
store.subscribe("asks", () => { renderDock(); renderRail(); });
// voice presence lives in the rack now (the live channel's meter + caption)
store.subscribe("voice", renderRackPanel);
// mic.level (~10Hz) drives ONLY the meter + VU slots in place — a full
// panel re-render at that rate would trash the deck
store.subscribe("miclevel", () => {
  setMicLevel(store.micLevel);
  setVuLevel(store.micLevel);
});
// speech.sentence fires per sentence — the rack updates every time,
// but the rail only cares WHO is speaking (eq marker), and the dock
// only when the transcript is showing (karaoke lives there).
let lastSpeakerWho = null;
store.subscribe("speaking", () => {
  renderRackPanel();
  const who = store.speaking && store.speaking.who;
  if (who !== lastSpeakerWho) { lastSpeakerWho = who; renderRail(); }
  if (dockTab === "transcript") renderDock();
});
store.subscribe("transcript", () => { if (dockTab === "transcript") renderDock(); });
store.subscribe("mic", () => { renderRackPanel(); renderStatus(); });
store.subscribe("conn", renderConn);
store.subscribe("ticker", renderRackPanel);

renderStrip(); renderRail(); renderWork(); renderRackPanel(); renderDock();
renderStatus();
