// @ts-check
/**
 * One WebSocket to /v1/events (SPEC §10): snapshot first, then live events.
 * Self-heals with capped backoff. Commands ride the same socket with the
 * workbench token in the URL (§8.5). No replay — a reconnect re-snapshots.
 */

/**
 * @param {import("./store.mjs").Store} store
 * @param {{onCommandReply?: (m:any)=>void, onEvent?: (env:any)=>void,
 *   commandTimeoutMs?: number}} [opts]
 */
export function connectBus(store, opts = {}) {
  const onCommandReply = opts.onCommandReply;
  const wb = (window.__VOCO__ || {}).wb || "";
  let ws = /** @type {?WebSocket} */ (null);
  let backoff = 500;
  let reqId = 0;
  let reconciling = false;
  const commandTimeoutMs = opts.commandTimeoutMs ?? 60000;
  /** @type {Map<string, {resolve:Function, reject:Function}>} */
  const pending = new Map();

  function url() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const q = wb ? "?wb=" + encodeURIComponent(wb) : "";
    return `${proto}//${location.host}/v1/events${q}`;
  }

  // The wb token is minted per daemon BOOT: after a restart every open
  // tab holds a stale token and would retry forever. A socket that dies
  // without ever delivering a snapshot while HTTP still answers is that
  // signature — reload once (fresh shell = fresh token); a sessionStorage
  // stamp guards against reload loops, and a genuinely down server just
  // keeps the normal retry cadence.
  let sawSnapshot = false;
  async function probeStaleToken() {
    try {
      const r = await fetch("/", { method: "HEAD" });
      if (!r.ok) return;
    } catch { return; } // server down — the retry loop is correct
    const last = Number(sessionStorage.getItem("voco.reloaded") || 0);
    if (Date.now() - last < 10000) {
      store.staleToken = true; // reloading didn't help — tell the human
      store._notify("conn");
      return;
    }
    sessionStorage.setItem("voco.reloaded", String(Date.now()));
    location.reload();
  }

  function open() {
    sawSnapshot = false;
    ws = new WebSocket(url());
    ws.onopen = () => { backoff = 500; store.connected = true; store._notify("conn"); };
    ws.onclose = () => {
      store.connected = false;
      store.retryAt = Date.now() + backoff; // the countdown's truth
      store._notify("conn");
      if (pending.size) reconciling = true;
      for (const { reject } of pending.values()) {
        reject(new OutcomeUnknownError("socket closed before command outcome"));
      }
      pending.clear();
      if (!sawSnapshot) probeStaleToken();
      setTimeout(open, backoff);
      backoff = Math.min(backoff * 2, 8000);
    };
    ws.onerror = () => ws && ws.close();
    ws.onmessage = (ev) => {
      let msg; try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.type === "snapshot") {
        sawSnapshot = true;
        reconciling = false;
        store.staleToken = false;
        store.applySnapshot(msg.payload);
        if (opts.onEvent) opts.onEvent(msg);
        return;
      }
      if (msg.type) {
        store.applyEvent(msg);
        if (opts.onEvent) opts.onEvent(msg); // console log tab tap (M6)
        return;
      }
      // Command reply: {id, ok, payload|error}
      const waiter = msg.id != null ? pending.get(msg.id) : undefined;
      if (waiter) {
        pending.delete(msg.id);
        msg.ok ? waiter.resolve(msg.payload) : waiter.reject(new Error(msg.error || "error"));
        if (onCommandReply) onCommandReply(msg);
      }
    };
  }

  /** @param {string} cmd @param {object} payload @returns {Promise<any>} */
  function command(cmd, payload = {}) {
    return new Promise((resolve, reject) => {
      if (reconciling)
        return reject(new OutcomeUnknownError(
          "previous operation outcome is unknown; wait for resynchronization",
        ));
      if (!ws || ws.readyState !== WebSocket.OPEN)
        return reject(new Error("not connected"));
      const id = "c" + ++reqId;
      pending.set(id, { resolve, reject });
      ws.send(JSON.stringify({ id, cmd, payload }));
      setTimeout(() => {
        if (pending.has(id)) {
          pending.delete(id);
          reconciling = true;
          reject(new OutcomeUnknownError(
            "operation may still complete; wait for resynchronization before retrying",
          ));
          if (ws) ws.close();
        }
      }, commandTimeoutMs);
    });
  }

  open();
  return { command };
}

export class OutcomeUnknownError extends Error {
  constructor(message) {
    super(message);
    this.name = "OutcomeUnknownError";
    this.code = "outcome_unknown";
  }
}
