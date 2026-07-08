// @ts-check
/**
 * The presence strip (DESIGN-DECK U1; U2R mount-once rebuild) — the
 * voice's permanent home: orb (attention ring; click cycles), the
 * mic-holder cell (ALWAYS named — ADR-0001), the caption slot, the ONE
 * input, the agent-speaking slot, ■ interrupt, ⚙ settings.
 *
 * MOUNT-ONCE (reference architecture, diff-annotate): the strip's DOM
 * is built exactly once; every later render UPDATES slots in place.
 * The layout is a fixed grid, so slots appearing/emptying never shift
 * their neighbors — and the input is a persistent element, so typing
 * survives every voice event (a rebuild used to eat the user's text).
 *
 * Hold-for-PTT is deliberately NOT wired: the daemon has no
 * ptt.press/release command yet — faking it in the UI would violate
 * the honest-signal rule. It lands with that command.
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

/** @type {?{orb:HTMLElement, orbLabel:HTMLElement, micHolder:HTMLElement,
 *   caption:HTMLElement, input:HTMLInputElement, speaking:HTMLElement}} */
let dom = null;
/** Latest render context — persistent handlers read through this. */
let live = /** @type {any} */ (null);

function buildOnce(strip) {
  const orb = el("div", { class: "orb" });
  orb.addEventListener("click", async () => {
    const attention = (live.store.mic || {}).attention || "wake";
    const next = ATTENTION_CYCLE[
      (ATTENTION_CYCLE.indexOf(attention) + 1) % ATTENTION_CYCLE.length];
    try { await live.ctx.command("mic.set", { attention: next }); }
    catch (e) { live.ctx.toast("attention: " + msg(e), true); }
  });
  const orbLabel = el("span", { class: "orb-label" });
  const micHolder = el("span", { class: "mic-holder",
    title: "who hears you — click an agent in the rail to move the mic" });
  const caption = el("div", { class: "caption", "aria-live": "polite" });
  const input = /** @type {HTMLInputElement} */ (el("input", {
    class: "deck-input", type: "text", "aria-label": "type as speech" }));
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
  const speaking = el("div", { class: "speaking-slot" });
  const ctl = el("div", { class: "pctl" },
    el("button", { class: "warm", text: "■",
      title: "interrupt: barge-in + Escape to the active agent",
      onclick: async () => {
        try { await live.ctx.command("interrupt", {}); }
        catch (e) { live.ctx.toast("interrupt: " + msg(e), true); }
      } }),
    el("button", { text: "⚙", title: "settings",
      onclick: () => live.ctx.onSettings() }));
  strip.append(
    el("div", { class: "orb-wrap" }, orb, orbLabel),
    micHolder, caption, input, speaking, ctl);
  return { orb, orbLabel, micHolder, caption, input, speaking };
}

/**
 * @param {HTMLElement} strip
 * @param {import("./store.mjs").Store} store
 * @param {{command:(cmd:string, payload?:object)=>Promise<any>,
 *   onFull:(target:"you"|"agent")=>void, toast:(msg:string, sticky?:boolean)=>void,
 *   onSettings:()=>void}} ctx
 */
export function renderPresence(strip, store, ctx) {
  live = { store, ctx };
  if (!dom || !strip.contains(dom.orb)) {
    strip.replaceChildren();
    dom = buildOnce(strip);
  }
  const mic = store.mic || {};
  const attention = mic.attention || "wake";
  const offline = !store.connected;
  const hearing = HEARING.has(store.turnState);
  const activeName = store._nameOf(store.activeSession);

  // orb + label: class/text updates only — never rebuilt
  dom.orb.className = "orb a-" + attention
    + (hearing ? " hearing" : "") + (offline ? " off" : "");
  dom.orb.title =
    `attention: ${attention} — click cycles muted → wake → always`;
  dom.orbLabel.textContent = attention;

  dom.micHolder.textContent = "→ " + (activeName || "—");

  // caption slot: the one slot whose CONTENT is replaced (it is the
  // live region; everything around it stays put)
  const cap = dom.caption;
  cap.replaceChildren();
  if (offline) {
    cap.append(el("span", { class: "cap-off" },
      el("span", { class: "rdot" }),
      el("span", { text: "daemon unreachable — reconnecting" })));
  } else if (hearing) {
    const bars = el("span", { class: "bars" });
    for (let i = 0; i < 5; i++) bars.append(el("i"));
    cap.append(bars, el("span", { class: "lstn", text: "listening" }));
    if (store.ticker)
      cap.append(el("span", { class: "cap-text", text: store.ticker }));
  } else if (store.lastRouted) {
    const r = store.lastRouted;
    cap.append(
      el("span", { class: "cap-text", text: "“" + r.text + "”" }),
      el("button", { class: "cap-more", text: "full",
        onclick: () => live.ctx.onFull("you") }),
      r.route ? el("span", { class: "route-chip", text: "→ " + r.route }) : null);
  }

  // the ONE input: placeholder/disabled only — value and focus SURVIVE
  dom.input.placeholder = activeName
    ? `say “deck …” or type — routes to ${activeName}`
    : "say “deck …” or type";
  dom.input.disabled = offline;

  // speaking slot: a permanent grid cell — content toggles, layout doesn't
  const sp = store.speaking;
  dom.speaking.replaceChildren();
  if (sp && sp.who) {
    const eq = el("span", { class: "eq" });
    for (let i = 0; i < 3; i++) eq.append(el("i"));
    dom.speaking.append(el("div", { class: "speaking",
      title: "click to jump to the speaker" },
      eq,
      el("span", { class: "speak-who", text: sp.who }),
      el("span", { class: "speak-text",
        text: "“" + (sp.sentence || sp.text || "") + "”" }),
      el("button", { class: "cap-more", text: "full",
        onclick: (e) => { e.stopPropagation(); live.ctx.onFull("agent"); } }),
      el("button", { class: "stopbtn", text: "■ stop",
        onclick: async (e) => {
          e.stopPropagation();
          try { await live.ctx.command("interrupt", {}); }
          catch (err) { live.ctx.toast("stop: " + msg(err), true); }
        } })));
  }
}

const msg = (e) => (e instanceof Error ? e.message : String(e));
