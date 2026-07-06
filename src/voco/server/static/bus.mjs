// @ts-check
/**
 * One WebSocket to /v1/events (SPEC §10): snapshot first, then live events.
 * Self-heals with capped backoff. Commands ride the same socket with the
 * workbench token in the URL (§8.5). No replay — a reconnect re-snapshots.
 */

/**
 * @param {import("./store.mjs").Store} store
 * @param {{onCommandReply?: (m:any)=>void}} [opts]
 */
export function connectBus(store, opts = {}) {
  const onCommandReply = opts.onCommandReply;
  const wb = (window.__VOCO__ || {}).wb || "";
  let ws = /** @type {?WebSocket} */ (null);
  let backoff = 500;
  let reqId = 0;
  /** @type {Map<string, {resolve:Function, reject:Function}>} */
  const pending = new Map();

  function url() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const q = wb ? "?wb=" + encodeURIComponent(wb) : "";
    return `${proto}//${location.host}/v1/events${q}`;
  }

  function open() {
    ws = new WebSocket(url());
    ws.onopen = () => { backoff = 500; store.connected = true; store._notify("conn"); };
    ws.onclose = () => {
      store.connected = false; store._notify("conn");
      for (const { reject } of pending.values()) reject(new Error("socket closed"));
      pending.clear();
      setTimeout(open, backoff);
      backoff = Math.min(backoff * 2, 8000);
    };
    ws.onerror = () => ws && ws.close();
    ws.onmessage = (ev) => {
      let msg; try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.type === "snapshot") { store.applySnapshot(msg.payload); return; }
      if (msg.type) { store.applyEvent(msg); return; }
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
      if (!ws || ws.readyState !== WebSocket.OPEN)
        return reject(new Error("not connected"));
      const id = "c" + ++reqId;
      pending.set(id, { resolve, reject });
      ws.send(JSON.stringify({ id, cmd, payload }));
      setTimeout(() => {
        if (pending.has(id)) { pending.delete(id); reject(new Error("timeout")); }
      }, 15000);
    });
  }

  open();
  return { command };
}
