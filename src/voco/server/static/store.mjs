// @ts-check
/**
 * Client state + subscribe seam (SPEC-WORKBENCH §7). Panels read the store
 * and subscribe by kind; they never reach into each other. The WS bus and
 * user actions mutate it; mutations notify subscribers of that kind.
 *
 * U1 additions (DESIGN-DECK): voice presence slices (turn state, the last
 * routed utterance, the speaking agent + karaoke sentence) and per-session
 * transcripts (fetched via session.transcript, marked stale by live events).
 *
 * @typedef {"gone"|"blocked"|"working"|"listening"|"stale"|"idle"} DisplayState
 * @typedef {{key:string, host:string, root:string, name:string, kind:string,
 *   repo:?string, branch:?string, common_dir:?string, pages:PageMeta[]}} Workspace
 * @typedef {{page_id:string, type:string, ref:string, title:string,
 *   scope:string, rev:number, pinned:boolean, closed:boolean,
 *   session_id:?string, call_name:?string}} PageMeta
 * @typedef {{session_id:string, name:string, display_name:string,
 *   state:string, display_state?:DisplayState, unread_digest:number,
 *   capabilities?:string[], host?:?string, root?:?string,
 *   queued?:number, say_tail?:{ts:number, text:string}[],
 *   screen_title?:?string, screen_markdown?:string}} Session
 */

/** @typedef {"workspaces"|"sessions"|"selection"|"mic"|"conn"|"ticker"|"findings"|"asks"|"voice"|"speaking"|"transcript"} Kind */

export class Store {
  constructor() {
    /** @type {Map<string, Workspace>} */ this.workspaces = new Map();
    /** @type {Map<string, Session>} */ this.sessions = new Map();
    // findings keyed by workspace key -> Map<finding_id, finding>
    /** @type {Map<string, Map<string, any>>} */ this.findings = new Map();
    // asks keyed the same way (workspace key -> Map<ask_id, ask>)
    /** @type {Map<string, Map<string, any>>} */ this.asks = new Map();
    this.selectedAgent = /** @type {?string} */ (null); // session_id
    this.activeSession = /** @type {?string} */ (null);
    this.selectedWorkspace = /** @type {?string} */ (null);
    this.selectedPage = /** @type {?string} */ (null);
    this.mic = /** @type {any} */ ({});
    this.connected = false;
    this.ticker = "";
    // ---- voice presence (U1) ------------------------------------------------
    // The turn machine's public state: idle|capturing|holding|routing|reopenable.
    this.turnState = "idle";
    // The last routed utterance — the strip's cap-final + the full jump.
    /** @type {?{text:string, origin:string, route:?string, ts:number}} */
    this.lastRouted = null;
    // Who is speaking aloud right now (+ the sentence being voiced).
    /** @type {?{who:?string, text:?string, sentence:?string, index:?number}} */
    this.speaking = null;
    // ---- transcripts (U1): session_id -> {inputs:[], says:[], stale:bool} ---
    /** @type {Map<string, {inputs:any[], says:any[], stale:boolean}>} */
    this.transcripts = new Map();
    /** @type {Map<Kind, Set<Function>>} */ this._subs = new Map();
  }

  /** @param {string} wsKey @returns {any[]} */
  findingsFor(wsKey) {
    const m = this.findings.get(wsKey);
    return m ? [...m.values()] : [];
  }

  /** @param {string} wsKey @returns {any[]} oldest first */
  asksFor(wsKey) {
    const m = this.asks.get(wsKey);
    return m ? [...m.values()].sort((a, b) => a.created_ts - b.created_ts) : [];
  }

  /** @param {Kind} kind @param {Function} fn @returns {() => void} */
  subscribe(kind, fn) {
    if (!this._subs.has(kind)) this._subs.set(kind, new Set());
    const set = /** @type {Set<Function>} */ (this._subs.get(kind));
    set.add(fn);
    return () => set.delete(fn);
  }

  /** @param {Kind[]} kinds */
  _notify(...kinds) {
    for (const k of kinds)
      for (const fn of this._subs.get(k) || []) {
        try { fn(); } catch (e) { console.error("subscriber", k, e); }
      }
  }

  // ---- snapshot + events ----------------------------------------------------

  applySnapshot(snap) {
    this.sessions.clear();
    for (const s of snap.sessions || []) this.sessions.set(s.session_id, s);
    this.activeSession = snap.active_session ?? null;
    this.workspaces.clear();
    for (const w of snap.workspaces || []) this.workspaces.set(w.key, w);
    // A snapshot means a (re)connect: mutations may have been missed, so
    // the lazy caches must refetch (the selection notify triggers it).
    this.findings.clear();
    this.asks.clear();
    for (const t of this.transcripts.values()) t.stale = true;
    this.mic = snap.mic || {};
    if (!this.selectedWorkspace && this.workspaces.size)
      this.selectWorkspace([...this.workspaces.keys()][0]);
    this._notify("workspaces", "sessions", "mic", "selection", "transcript");
  }

  /** Route a bus event to the right slice. Unknown types are ignored. */
  applyEvent(env) {
    const p = env.payload || {};
    switch (env.type) {
      case "workspace.updated": {
        const ex = this.workspaces.get(p.key);
        if (ex) Object.assign(ex, { repo: p.repo, branch: p.branch,
          common_dir: p.common_dir, name: p.name });
        else this.workspaces.set(p.key,
          { ...p, pages: [] });
        if (!this.selectedWorkspace) this.selectWorkspace(p.key);
        this._notify("workspaces");
        break;
      }
      case "page.updated": {
        this._applyPage(p);
        this._notify("workspaces", "selection");
        break;
      }
      case "finding.added":
      case "finding.updated": {
        // Full state rides every finding event (last-writer-wins, §4.1).
        const key = p.workspace;
        if (!this.findings.has(key)) this.findings.set(key, new Map());
        /** @type {Map<string, any>} */ (this.findings.get(key))
          .set(p.finding_id, p);
        this._notify("findings");
        break;
      }
      case "ask.created":
      case "ask.answered": {
        // Same convergence model as findings: full state, last write wins.
        const key = p.workspace;
        if (!this.asks.has(key)) this.asks.set(key, new Map());
        /** @type {Map<string, any>} */ (this.asks.get(key)).set(p.ask_id, p);
        this._notify("asks");
        break;
      }
      case "screen.updated": { // page.updated re-renders the page view;
        const s = this.sessions.get(p.session_id); // the agent card too
        if (s) {
          s.screen_markdown = p.markdown ?? s.screen_markdown;
          s.screen_title = p.title ?? s.screen_title;
          this._notify("sessions");
        }
        break;
      }
      case "session.attached":
      case "session.state":
      case "session.detached":
      case "session.activated":
      case "digest.updated":
      case "pane.hint": // carries a fresh display_state (dot precedence)
        this._applySessionEvent(env.type, p);
        this._notify("sessions");
        break;
      case "mic.state":
        this.mic = { ...this.mic, ...p };
        this._notify("mic");
        break;
      // ---- voice presence (U1) ---------------------------------------------
      case "turn.state":
        this.turnState = p.state || "idle";
        this._notify("voice");
        break;
      case "stt.final":
        this.lastRouted = { text: p.text || "", origin: "voice",
          route: this._nameOf(this.activeSession), ts: Date.now() / 1000 };
        this.ticker = "";
        this._staleTranscript(this.activeSession);
        this._notify("voice", "ticker", "transcript");
        break;
      case "route.decision":
        if (this.lastRouted && p.kind === "answer")
          this.lastRouted.route = "first mate";
        this._notify("voice");
        break;
      case "input.queued":
        this._staleTranscript(p.session_id);
        this._notify("transcript", "sessions");
        break;
      case "agent.say":
        this._staleTranscript(p.session_id);
        this._notify("transcript");
        break;
      case "speech.started":
        if (p.source === "agent")
          this.speaking = { who: p.who ?? null, text: p.text ?? null,
            sentence: null, index: null };
        this._notify("speaking");
        break;
      case "speech.sentence":
        this.speaking = { ...(this.speaking || { who: p.who, text: null }),
          who: p.who, sentence: p.text, index: p.index };
        this._notify("speaking");
        break;
      case "speech.finished":
      case "speech.interrupted":
        if (p.source === "agent") this.speaking = null;
        this._notify("speaking");
        break;
      case "stt.partial": // declared-but-unemitted today; lights up when
        this.ticker = p.text || ""; // a streaming STT lands
        this._notify("ticker");
        break;
    }
  }

  _applyPage(p) {
    const ws = this.workspaces.get(p.workspace);
    if (!ws) return;
    const meta = {
      page_id: p.page_id, type: p.type, ref: p.ref, title: p.title,
      scope: p.scope, rev: p.rev, pinned: p.pinned, closed: p.closed,
      session_id: p.session_id, call_name: p.call_name,
    };
    const i = ws.pages.findIndex((x) => x.page_id === p.page_id);
    if (i >= 0) ws.pages[i] = meta; else ws.pages.push(meta);
    // Auto-select the first page of the selected workspace.
    if (p.workspace === this.selectedWorkspace && !this._selectedPageLive())
      this.selectedPage = p.page_id;
  }

  _nameOf(sessionId) {
    const s = sessionId && this.sessions.get(sessionId);
    return s ? s.name : null;
  }

  _staleTranscript(sessionId) {
    const t = sessionId && this.transcripts.get(sessionId);
    if (t) t.stale = true;
  }

  /** @param {string} sessionId */
  transcriptFor(sessionId) {
    return this.transcripts.get(sessionId) || null;
  }

  setTranscript(sessionId, data) {
    this.transcripts.set(sessionId, {
      inputs: data.inputs || [], says: data.says || [], stale: false,
    });
    this._notify("transcript");
  }

  _applySessionEvent(type, p) {
    if (type === "session.detached") {
      this.sessions.delete(p.session_id);
      this.transcripts.delete(p.session_id);
      if (this.selectedAgent === p.session_id) this.selectedAgent = null;
      return;
    }
    if (type === "session.activated") { this.activeSession = p.session_id; return; }
    const s = this.sessions.get(p.session_id) || /** @type {Session} */ ({
      session_id: p.session_id, name: p.name || "?", display_name: p.name || "?",
      state: "idle", unread_digest: 0,
    });
    if (p.state) s.state = p.state;
    if (p.display_state) s.display_state = p.display_state;
    if (typeof p.unread === "number") s.unread_digest = p.unread;
    // session.attached carries capabilities + home identity (host/root)
    // so workspace-scoped checks stay live between snapshots.
    if (Array.isArray(p.capabilities)) s.capabilities = p.capabilities;
    if (p.host !== undefined) s.host = p.host;
    if (p.root !== undefined) s.root = p.root;
    this.sessions.set(p.session_id, s);
  }

  // ---- selection ------------------------------------------------------------

  selectWorkspace(key) {
    this.selectedWorkspace = key;
    const ws = this.workspaces.get(key);
    const open = ws && ws.pages.filter((p) => !p.closed);
    this.selectedPage = open && open.length ? open[0].page_id : null;
    this._notify("selection");
  }

  selectPage(pageId) {
    this.selectedPage = pageId;
    this._notify("selection");
  }

  _selectedPageLive() {
    const ws = this.workspaces.get(this.selectedWorkspace || "");
    return ws && ws.pages.some(
      (p) => p.page_id === this.selectedPage && !p.closed);
  }

  selectedWs() { return this.workspaces.get(this.selectedWorkspace || ""); }
}
