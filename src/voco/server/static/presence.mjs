// @ts-check
/**
 * The command bar — ADE chrome only (index7 FL push): one 36px row of
 * [voco ● host] · spacer · [⚙ settings].
 *
 * The one input, route display, and ■ interrupt moved to the deck
 * header (rack.mjs): audio belongs on the instrument, not the IDE
 * shell — and the deck header survives minimize, so they stay
 * reachable. Settings stays here: it is app-level, not deck-level.
 *
 * MOUNT-ONCE (kept from U1): the bar's DOM is built exactly once; every
 * later render updates slots in place. The identity cell is the daemon
 * truth: LED + host, with the honest reconnect countdown (bus retryAt)
 * while offline.
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

/** @type {?{led:HTMLElement, host:HTMLElement}} */
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
  const keys = el("div", { class: "cmd-cell cmd-keys" },
    el("button", { class: "cmd-btn", text: "⚙", title: "settings",
      onclick: () => live.ctx.onSettings() }));
  bar.append(idcell, el("div", { class: "cmd-spacer" }), keys);
  return { led, host };
}

/**
 * @param {HTMLElement} bar
 * @param {import("./store.mjs").Store} store
 * @param {{onSettings:()=>void}} ctx
 */
export function renderPresence(bar, store, ctx) {
  live = { store, ctx };
  if (!dom || !bar.contains(dom.led)) {
    bar.replaceChildren();
    dom = buildOnce(bar);
  }
  const offline = !store.connected;

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
}
