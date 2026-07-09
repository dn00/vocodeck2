// @ts-check
/**
 * The command bar (CONSOLE mk3, M1; BUILD-CONSOLE.md) — one 36px row of
 * cells: [voco ● host] [caption*] [> the ONE input · route → holder]
 * [speaking*] [keys: attention · ■ interrupt · ⚙].
 *
 * MOUNT-ONCE (kept from U1): the bar's DOM is built exactly once; every
 * later render updates slots in place, so the input's value and focus
 * survive every voice event.
 *
 * (*) Interim cells: the live caption and the agent-speaking slot move
 * to the channel rack when M5 lands; they ride here until then so the
 * functions never disappear between milestones.
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

const ATTENTION_CYCLE = ["muted", "wake", "always"];
const HEARING = new Set(["capturing", "holding", "routing"]);

/** @type {?{led:HTMLElement, host:HTMLElement, caption:HTMLElement,
 *   input:HTMLInputElement, route:HTMLElement, attn:HTMLElement,
 *   speaking:HTMLElement}} */
let dom = null;
/** Latest render context — persistent handlers read through this. */
let live = /** @type {any} */ (null);

function buildOnce(bar) {
  const led = el("span", { class: "cmd-led" });
  const host = el("span", { class: "cmd-host", text: location.host });
  const idcell = el("div", { class: "cmd-cell cmd-id" },
    el("span", { class: "cmd-app", text: "voco" }), led, host);

  const caption = el("div", { class: "cmd-cell cmd-caption",
    "aria-live": "polite" });

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
    title: "who hears you — click an agent in the tree to move the mic" });
  const prompt = el("div", { class: "cmd-prompt" },
    el("span", { class: "cmd-gt", text: ">" }), input, route);

  const speaking = el("div", { class: "cmd-cell cmd-speaking" });

  const attn = el("span", { class: "cmd-attn" });
  attn.addEventListener("click", async () => {
    const attention = (live.store.mic || {}).attention || "wake";
    const next = ATTENTION_CYCLE[
      (ATTENTION_CYCLE.indexOf(attention) + 1) % ATTENTION_CYCLE.length];
    try { await live.ctx.command("mic.set", { attention: next }); }
    catch (e) { live.ctx.toast("attention: " + msg(e), true); }
  });
  const keys = el("div", { class: "cmd-cell cmd-keys" },
    attn,
    el("button", { class: "cmd-btn warm", text: "■",
      title: "interrupt: barge-in + Escape to the active agent",
      onclick: async () => {
        try { await live.ctx.command("interrupt", {}); }
        catch (e) { live.ctx.toast("interrupt: " + msg(e), true); }
      } }),
    el("button", { class: "cmd-btn", text: "⚙", title: "settings",
      onclick: () => live.ctx.onSettings() }));

  bar.append(idcell, caption, prompt, speaking, keys);
  return { led, host, caption, input, route, attn, speaking };
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
  const mic = store.mic || {};
  const attention = mic.attention || null;
  const offline = !store.connected;
  const hearing = HEARING.has(store.turnState);
  const activeName = store._nameOf(store.activeSession);

  // identity cell: the LED is the daemon truth
  dom.led.className = "cmd-led " + (offline ? "off" : "on");
  dom.host.textContent = offline ? "reconnecting…" : location.host;
  dom.host.classList.toggle("down", offline);

  // caption (interim; live region): listening / last routed / empty
  const cap = dom.caption;
  cap.replaceChildren();
  if (!offline && hearing) {
    const bars = el("span", { class: "bars" });
    for (let i = 0; i < 5; i++) bars.append(el("i"));
    cap.append(bars, el("span", { class: "lstn", text: "listening" }));
    if (store.ticker)
      cap.append(el("span", { class: "cap-text", text: store.ticker }));
  } else if (!offline && store.lastRouted) {
    const r = store.lastRouted;
    cap.append(
      el("span", { class: "cap-text", text: "“" + r.text + "”" }),
      el("button", { class: "cap-more", text: "full",
        onclick: () => live.ctx.onFull("you") }),
      r.route ? el("span", { class: "cap-route", text: "→ " + r.route }) : null);
  }
  cap.classList.toggle("empty", !cap.childElementCount);

  // the ONE input: placeholder/disabled only — value and focus SURVIVE
  dom.input.placeholder = "say “deck …” or type";
  dom.input.disabled = offline;
  dom.route.textContent = activeName ? "route → " + activeName : "route → —";
  dom.route.classList.toggle("none", !activeName);

  // attention word: mic truth; click cycles (master block takes over in M5)
  dom.attn.textContent = attention || "headless";
  dom.attn.className = "cmd-attn" + (attention ? "" : " none")
    + (attention === "muted" ? " muted" : "");
  dom.attn.title = attention
    ? `attention: ${attention} — click cycles muted → wake → always`
    : "no voice loop (daemon started without audio)";

  // speaking (interim): who + sentence + full + stop
  const sp = store.speaking;
  dom.speaking.replaceChildren();
  if (sp && sp.who) {
    const eq = el("span", { class: "eq" });
    for (let i = 0; i < 3; i++) eq.append(el("i"));
    dom.speaking.append(
      eq,
      el("span", { class: "speak-who", text: sp.who }),
      el("span", { class: "speak-text",
        text: "“" + (sp.sentence || sp.text || "") + "”" }),
      el("button", { class: "cap-more", text: "full",
        onclick: () => live.ctx.onFull("agent") }),
      el("button", { class: "cmd-btn stop", text: "■",
        title: "stop speaking",
        onclick: async () => {
          try { await live.ctx.command("interrupt", {}); }
          catch (err) { live.ctx.toast("stop: " + msg(err), true); }
        } }));
  }
  dom.speaking.classList.toggle("empty", !sp || !sp.who);
}

const msg = (e) => (e instanceof Error ? e.message : String(e));
