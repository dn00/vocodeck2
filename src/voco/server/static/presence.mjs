// @ts-check
/**
 * The presence strip (DESIGN-DECK U1) — the voice's permanent home:
 * orb (attention = ring; click cycles muted → wake → always), the
 * caption slot (listening → the routed one-liner + full + route chip),
 * the ONE input (typing = speaking), and the agent-speaking slot
 * (who + current sentence + full + ■ stop). Sound is the single global
 * surface: this strip never scopes to the selected agent.
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

/**
 * @param {HTMLElement} strip
 * @param {import("./store.mjs").Store} store
 * @param {{command:(cmd:string, payload?:object)=>Promise<any>,
 *   onFull:(target:"you"|"agent")=>void, toast:(msg:string, sticky?:boolean)=>void}} ctx
 */
export function renderPresence(strip, store, ctx) {
  strip.replaceChildren();
  const mic = store.mic || {};
  const attention = mic.attention || "wake";
  const offline = !store.connected;
  const hearing = HEARING.has(store.turnState);
  const activeName = store._nameOf(store.activeSession);

  // ---- orb: the standing proof the deck listens -----------------------------
  const orb = el("div", {
    class: "orb a-" + attention + (hearing ? " hearing" : "") + (offline ? " off" : ""),
    title: `attention: ${attention} — click cycles muted → wake → always`,
    onclick: async () => {
      const next = ATTENTION_CYCLE[
        (ATTENTION_CYCLE.indexOf(attention) + 1) % ATTENTION_CYCLE.length];
      try { await ctx.command("mic.set", { attention: next }); }
      catch (e) { ctx.toast("attention: " + msg(e), true); }
    },
  });
  strip.append(el("div", { class: "orb-wrap" }, orb,
    el("span", { class: "orb-label", text: attention })));

  // ---- caption slot (aria-live: it IS the live region) -----------------------
  const cap = el("div", { class: "caption", "aria-live": "polite" });
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
        onclick: () => ctx.onFull("you") }),
      r.route ? el("span", { class: "route-chip", text: "→ " + r.route }) : null);
  }
  strip.append(cap);

  // ---- the ONE input: typing = speaking --------------------------------------
  const input = /** @type {HTMLInputElement} */ (el("input", {
    class: "deck-input", type: "text", "aria-label": "type as speech",
    placeholder: activeName
      ? `say “deck …” or type — routes to ${activeName}`
      : "say “deck …” or type",
  }));
  if (offline) input.disabled = true;
  input.addEventListener("keydown", async (e) => {
    if (e.key !== "Enter") return;
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    try {
      await ctx.command("say_as_user", { text });
      store.lastRouted = { text, origin: "typed",
        route: store._nameOf(store.activeSession), ts: Date.now() / 1000 };
      store._staleTranscript(store.activeSession);
      store._notify("voice", "transcript");
    } catch (err) { ctx.toast("send failed: " + msg(err), true); }
  });
  strip.append(input);

  // ---- speaking slot: who says what, right now --------------------------------
  if (store.speaking && store.speaking.who) {
    const sp = store.speaking;
    const eq = el("span", { class: "eq" });
    for (let i = 0; i < 3; i++) eq.append(el("i"));
    strip.append(el("div", { class: "speaking", title: "click to jump to the speaker" },
      eq,
      el("span", { class: "speak-who", text: sp.who }),
      el("span", { class: "speak-text",
        text: "“" + (sp.sentence || sp.text || "") + "”" }),
      el("button", { class: "cap-more", text: "full",
        onclick: (e) => { e.stopPropagation(); ctx.onFull("agent"); } }),
      el("button", { class: "stopbtn", text: "■ stop",
        onclick: async (e) => {
          e.stopPropagation();
          try { await ctx.command("interrupt", {}); }
          catch (err) { ctx.toast("stop: " + msg(err), true); }
        } })));
  }

  // ---- controls ---------------------------------------------------------------
  strip.append(el("div", { class: "pctl" },
    el("button", { class: "warm", text: "■",
      title: "interrupt: barge-in + Escape to the active agent",
      onclick: async () => {
        try { await ctx.command("interrupt", {}); }
        catch (e) { ctx.toast("interrupt: " + msg(e), true); }
      } })));
}

const msg = (e) => (e instanceof Error ? e.message : String(e));
