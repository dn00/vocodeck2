// @ts-check
/**
 * Terminal page (SPEC-WORKBENCH §5, W4). Cell-driven, two modes:
 *
 * - "stream" (pty): a live xterm over the /v1/term WS. Interactive on
 *   focus — click to focus, focused keys go straight to the pty, and
 *   the holder shows a visible focus ring (grill decision 23).
 * - "mirror" (tmux): read-only capture polling via session.peek, with
 *   an input row honestly labeled "send as user input" (it dispatches
 *   through say_as_user — it does NOT type into the pane; real typing
 *   is `tmux attach`).
 *
 * Live terminals are cached by page_id and their DOM re-attached on
 * re-render: dock/finding updates re-render the editor, and a terminal
 * must not flicker or reconnect for every chip flip.
 */

const el = (tag, attrs = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;
    else if (k === "onclick") n.addEventListener("click", v);
    else if (v != null) n.setAttribute(k, String(v));
  }
  for (const kid of kids) if (kid) n.append(kid);
  return n;
};

let cssInjected = false;
function ensureCss() {
  if (cssInjected) return;
  cssInjected = true;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = "/static/vendor/xterm.css";
  document.head.append(link);
}

/** @type {Map<string, {holder: HTMLElement, fit: any, ws: WebSocket}>} */
const live = new Map();

/**
 * @param {HTMLElement} view
 * @param {{page_id:string}} page
 * @param {{mode:string, call_name:?string, session_id:?string}} content
 * @param {{wb:string, command:(cmd:string, payload?:object)=>Promise<any>}} ctx
 */
export async function renderTerminal(view, page, content, ctx) {
  if (content.mode === "stream" && content.session_id) {
    return streamTerminal(view, page, content, ctx);
  }
  return mirrorTerminal(view, content, ctx);
}

async function streamTerminal(view, page, content, ctx) {
  ensureCss();
  const cached = live.get(page.page_id);
  if (cached && cached.ws.readyState <= WebSocket.OPEN) {
    view.replaceChildren(cached.holder); // re-attach, no reconnect
    requestAnimationFrame(() => cached.fit.fit());
    return;
  }
  live.delete(page.page_id);
  const [{ Terminal }, { FitAddon }] = await Promise.all([
    import("./vendor/xterm.mjs"),
    import("./vendor/xterm-addon-fit.mjs"),
  ]);
  const holder = el("div", { class: "term-holder", tabindex: "-1" });
  view.replaceChildren(holder);
  const term = new Terminal({ fontSize: 13, scrollback: 5000 });
  const fit = new FitAddon();
  term.loadAddon(fit);
  term.open(holder);
  fit.fit();

  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const q = ctx.wb ? "?wb=" + encodeURIComponent(ctx.wb) : "";
  const ws = new WebSocket(
    `${proto}//${location.host}/v1/term/${content.session_id}${q}`);
  ws.binaryType = "arraybuffer";
  ws.onopen = () =>
    ws.send(JSON.stringify({ resize: { cols: term.cols, rows: term.rows } }));
  ws.onmessage = (ev) => {
    if (ev.data instanceof ArrayBuffer) term.write(new Uint8Array(ev.data));
  };
  ws.onclose = () => {
    term.write("\r\n\x1b[2m[terminal closed]\x1b[0m\r\n");
    live.delete(page.page_id);
  };
  const encoder = new TextEncoder();
  term.onData((d) => {
    if (ws.readyState === WebSocket.OPEN) ws.send(encoder.encode(d));
  });
  term.onResize(({ cols, rows }) => {
    if (ws.readyState === WebSocket.OPEN)
      ws.send(JSON.stringify({ resize: { cols, rows } }));
  });
  new ResizeObserver(() => {
    if (holder.isConnected) fit.fit();
  }).observe(holder);
  live.set(page.page_id, { holder, fit, ws });
}

async function mirrorTerminal(view, content, ctx) {
  const pre = el("pre", { class: "term-mirror", text: "…" });
  const note = el("div", { class: "term-note",
    text: "read-only mirror — real typing: tmux attach" });
  const input = /** @type {HTMLInputElement} */ (el("input", {
    class: "term-input", placeholder: "send as user input (say_as_user)…",
  }));
  input.addEventListener("keydown", async (e) => {
    if (e.key !== "Enter") return;
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    try { await ctx.command("say_as_user", { text }); }
    catch (err) { note.textContent = "send failed: " + err; }
  });
  view.replaceChildren(pre, el("div", { class: "term-mirror-bar" }, note, input));

  async function tick() {
    if (!pre.isConnected) return; // page navigated away: stop polling
    try {
      const r = await ctx.command("session.peek", { name: content.call_name });
      pre.textContent = r.text || "(empty)";
    } catch (e) { /* daemon busy/offline; keep the last frame */ }
    setTimeout(tick, 2500);
  }
  tick();
}
