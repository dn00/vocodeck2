// @ts-check
/**
 * The command bar (CONSOLE mk3, final form as of M5) — one 36px row of
 * cells: [voco ● host] [> the ONE input · route → holder] [keys: ■
 * interrupt · ⚙ settings].
 *
 * MOUNT-ONCE (kept from U1): the bar's DOM is built exactly once; every
 * later render updates slots in place, so the input's value and focus
 * survive every voice event.
 *
 * Voice presence (live caption, speaking line, attention control)
 * lives in the channel rack (rack.mjs) since M5.
 *
 * Deliberately absent (honest-signal rule): no ⌘K hint until the
 * palette exists (M9); no PTT hold/key hint until the daemon grows
 * ptt.press/release (post-skin backlog).
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

/** @type {?{led:HTMLElement, host:HTMLElement, input:HTMLInputElement,
 *   route:HTMLElement}} */
let dom = null;
/** Latest render context — persistent handlers read through this. */
let live = /** @type {any} */ (null);
/** Offline countdown ticker (runs only while disconnected). */
let retryTick = /** @type {any} */ (null);

function buildOnce(bar) {
  const led = el("span", { class: "cmd-led" });
  const host = el("span", { class: "cmd-host", text: location.host });
  const idcell = el("div", { class: "cmd-cell cmd-id" },
    el("span", { class: "cmd-app", text: "voco" }), led, host);

  const input = /** @type {HTMLInputElement} */ (el("input", {
    class: "cmd-input", type: "text", "aria-label": "type as speech" }));
  input.addEventListener("keydown", async (e) => {
    if (e.key !== "Enter") return;
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    const store = live.store;
    try {
      await live.ctx.command("say_as_user", { text });
      store.lastRouted = { text, origin: "typed",
        route: store._nameOf(store.activeSession), ts: Date.now() / 1000 };
      store._staleTranscript(store.activeSession);
      store._notify("voice", "transcript");
    } catch (err) { live.ctx.toast("send failed: " + msg(err), true); }
  });
  const route = el("span", { class: "cmd-route",
    title: "who hears you — click an agent or a channel's mic patch to move it" });
  const prompt = el("div", { class: "cmd-prompt" },
    el("span", { class: "cmd-gt", text: ">" }), input, route);

  const keys = el("div", { class: "cmd-cell cmd-keys" },
    el("button", { class: "cmd-btn warm", text: "■",
      title: "interrupt: barge-in + Escape to the active agent",
      onclick: async () => {
        try { await live.ctx.command("interrupt", {}); }
        catch (e) { live.ctx.toast("interrupt: " + msg(e), true); }
      } }),
    el("button", { class: "cmd-btn", text: "⚙", title: "settings",
      onclick: () => live.ctx.onSettings() }));

  bar.append(idcell, prompt, keys);
  return { led, host, input, route };
}

/**
 * @param {HTMLElement} bar
 * @param {import("./store.mjs").Store} store
 * @param {{command:(cmd:string, payload?:object)=>Promise<any>,
 *   onFull:(target:"you"|"agent")=>void, toast:(msg:string, sticky?:boolean)=>void,
 *   onSettings:()=>void}} ctx
 */
export function renderPresence(bar, store, ctx) {
  live = { store, ctx };
  if (!dom || !bar.contains(dom.input)) {
    bar.replaceChildren();
    dom = buildOnce(bar);
  }
  const offline = !store.connected;
  const activeName = store._nameOf(store.activeSession);

  // identity cell: the LED is the daemon truth; while offline the host
  // cell carries the honest reconnect countdown (bus retryAt)
  dom.led.className = "cmd-led " + (offline ? "off" : "on");
  dom.host.classList.toggle("down", offline);
  clearInterval(retryTick);
  const host = dom.host;
  if (offline && store.staleToken) {
    host.textContent = "daemon restarted — reload this tab";
  } else if (offline) {
    const update = () => {
      const s = Math.max(0, Math.ceil(((live.store.retryAt || 0) - Date.now()) / 1000));
      host.textContent = s > 0
        ? `daemon unreachable — retry in ${s}s` : "reconnecting…";
    };
    update();
    retryTick = setInterval(update, 500);
  } else {
    host.textContent = location.host;
  }

  // the ONE input: placeholder/disabled only — value and focus SURVIVE.
  // No voice loop = no "say" (audit #7: the placeholder must not offer
  // a voice path a headless daemon cannot hear).
  dom.input.placeholder = (store.mic && store.mic.attention)
    ? "say “deck …” or type"
    : "type — routes like speech";
  dom.input.disabled = offline;
  dom.route.textContent = activeName ? "route → " + activeName : "route → —";
  dom.route.classList.toggle("none", !activeName);
}

const msg = (e) => (e instanceof Error ? e.message : String(e));
