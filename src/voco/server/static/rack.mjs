// @ts-check
/**
 * THE DECK (mk4) — agents as cards laid left-to-right along the bottom,
 * like a real console's channel strips. vocodeck, full circle.
 *
 * Each card: LED · NAME · state+age, the MIC patch, a level meter, and
 * the agent's LAST UTTERANCE (the mic holder shows the live caption
 * instead while a turn is running). A dashed MASTER card anchors the
 * left end: hold-to-talk, attention (click-cycles), duplex, counts.
 *
 * Card body = VIEW the agent's work; only the MIC patch (or the tree's
 * agent row / a spoken phrase) moves the mic. The — / ▢ toggle
 * minimizes the deck to a 44px chip strip (persisted); big fleets
 * scroll horizontally, sorted mic-first then by attention.
 *
 * "listening" renders as "ready" (audit): the state means the agent is
 * parked at its listen call awaiting input — the WORD listening belongs
 * to the mic, not the agent.
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

// All four modes (audit #2): a cycle that skips ptt_only strands a
// ptt_only daemon after one click.
const ATTENTION_CYCLE = ["muted", "wake", "ptt_only", "always"];
const HEARING = new Set(["capturing", "holding", "routing"]);
const STATE_ORDER = { blocked: 0, working: 1, listening: 2, idle: 3,
  stale: 4, gone: 5 };
const stateWord = (st) => (st === "listening" ? "ready" : st);

// Hold-PTT state is MODULE-level: the deck re-renders on voice events
// mid-hold, so pointerup can land on a dead element — the document
// listener below is the release that always fires.
let pttHeld = false;
let pttReleaseFn = /** @type {?()=>void} */ (null);
document.addEventListener("pointerup", () => {
  if (pttHeld && pttReleaseFn) pttReleaseFn();
});

// Minimized mode persists; the toggle re-renders with the last args.
let deckMin = false;
try { deckMin = JSON.parse(localStorage.getItem("voco.deckMin") || "false"); }
catch { deckMin = false; }
let lastArgs = /** @type {?[HTMLElement, any, any]} */ (null);

// Ages: the store's state_ts (the event envelope's honest transition
// time) wins; the observed map is the fallback for sessions whose
// state never changed since this tab loaded.
const seen = new Map(); // session_id -> {state, since}
function fmtAge(secs) {
  if (secs < 5) return null;
  if (secs < 120) return Math.floor(secs) + "s";
  if (secs < 7200) return Math.floor(secs / 60) + "m";
  return Math.floor(secs / 3600) + "h";
}
function observedAge(s, state) {
  if (s.state_ts) return fmtAge(Date.now() / 1000 - s.state_ts);
  const rec = seen.get(s.session_id);
  if (!rec || rec.state !== state) {
    seen.set(s.session_id, { state, since: Date.now() });
    return rec ? "0s" : null; // fresh transition; unknown on first sight
  }
  return fmtAge((Date.now() - rec.since) / 1000);
}

function meter(live) {
  const m = el("span", { class: "meter" + (live ? " live" : " idle") });
  for (let i = 0; i < 6; i++) m.append(el("i", { class: i > 3 ? "a" : "" }));
  return m;
}

function lastSayOf(s) {
  const t = s.say_tail && s.say_tail.length
    ? s.say_tail[s.say_tail.length - 1] : null;
  return t && t.text ? t.text : null;
}

function patchBtn(s, isMic, ctx) {
  return el("span", { class: "patch" + (isMic ? " on" : ""),
    title: isMic ? "your mic is patched here" : `patch the mic to ${s.name}`,
    text: isMic ? "MIC" : "mic",
    onclick: (e) => { e.stopPropagation(); if (!isMic) ctx.selectAgent(s); } });
}

/**
 * @param {HTMLElement} deck
 * @param {import("./store.mjs").Store} store
 * @param {{command:(cmd:string, payload?:object)=>Promise<any>,
 *   selectAgent:(s:any)=>void, focusAgent:(s:any)=>void,
 *   stateOf:(s:any)=>string,
 *   onFull:(target:"you"|"agent")=>void,
 *   toast:(msg:string, sticky?:boolean)=>void}} ctx
 */
export function renderRack(deck, store, ctx) {
  lastArgs = [deck, store, ctx];
  deck.replaceChildren();
  deck.classList.toggle("min", deckMin);
  const sessions = [...store.sessions.values()].sort((a, b) =>
    (a.session_id === store.activeSession ? -1 : 0)
    - (b.session_id === store.activeSession ? -1 : 0)
    || (STATE_ORDER[ctx.stateOf(a)] ?? 9) - (STATE_ORDER[ctx.stateOf(b)] ?? 9)
    || a.name.localeCompare(b.name));
  const hearing = HEARING.has(store.turnState);
  const mic = store.mic || {};

  const toggle = el("span", { class: "tgl",
    title: deckMin ? "expand the deck" : "minimize the deck",
    text: deckMin ? "▢" : "—",
    onclick: () => {
      deckMin = !deckMin;
      localStorage.setItem("voco.deckMin", JSON.stringify(deckMin));
      if (lastArgs) renderRack(...lastArgs);
    } });
  deck.append(el("div", { class: "dhead caps" },
    el("span", { text: "DECK · " + sessions.length }), toggle));

  const cards = el("div", { class: "cards" });
  deck.append(cards);

  if (deckMin) {
    for (const s of sessions) {
      const isMic = s.session_id === store.activeSession;
      const isSel = s.session_id === store.selectedAgent;
      const state = ctx.stateOf(s);
      cards.append(el("span", {
        class: "chipcard" + (isMic ? " live" : "") + (isSel ? " sel" : ""),
        title: `view ${s.name}'s work — the mic stays put`,
        onclick: () => ctx.focusAgent(s) },
        el("span", { class: "dot " + state }),
        el("span", { class: "nm", text: s.name }),
        isMic ? el("span", { class: "micchip", text: "MIC" }) : "",
        s.queued ? el("span", { class: "q", text: "q" + s.queued }) : "",
        el("span", { class: "st", text: stateWord(state) })));
    }
    if (!sessions.length)
      cards.append(el("span", { class: "empty-note",
        text: "no agents — connect one and its card appears here" }));
    return;
  }

  // master card ---------------------------------------------------------------
  const canPtt = !!mic.attention && mic.attention !== "muted";
  const hold = el("span", { class: "hold" + (canPtt ? "" : " none")
    + (pttHeld ? " held" : ""),
    text: pttHeld ? "● open — release to send" : "● hold to talk",
    title: canPtt
      ? "hold to open the mic (Space works too, in ptt_only)"
      : "needs a voice loop, not muted" });
  if (canPtt) {
    pttReleaseFn = () => {
      if (!pttHeld) return;
      pttHeld = false;
      hold.classList.remove("held");
      hold.textContent = "● hold to talk";
      ctx.command("ptt.release").catch(() => {});
    };
    hold.addEventListener("pointerdown", (e) => {
      e.stopPropagation();
      if (pttHeld) return;
      pttHeld = true;
      hold.classList.add("held");
      hold.textContent = "● open — release to send";
      ctx.command("ptt.press")
        .catch((err) => { ctx.toast("ptt: " + msg(err), true); pttHeld = false; });
    });
  }
  const attn = el("span", { class: "v" + (mic.attention ? " hot" : ""),
    text: mic.attention || "headless" });
  if (mic.attention) {
    attn.classList.add("cyc");
    attn.title = `attention: ${mic.attention} — click cycles `
      + ATTENTION_CYCLE.join(" → ");
    attn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const next = ATTENTION_CYCLE[
        (ATTENTION_CYCLE.indexOf(mic.attention) + 1) % ATTENTION_CYCLE.length];
      try { await ctx.command("mic.set", { attention: next }); }
      catch (err) { ctx.toast("attention: " + msg(err), true); }
    });
  } else {
    attn.title = "no voice loop (daemon started without audio)";
  }
  const counts = { working: 0, listening: 0 };
  for (const s of store.sessions.values()) {
    const st = ctx.stateOf(s);
    if (st in counts) counts[st]++;
  }
  cards.append(el("div", { class: "card master" },
    hold,
    el("div", { class: "m-row" }, el("span", { text: "attention" }), attn),
    el("div", { class: "m-row" }, el("span", { text: "duplex" }),
      el("span", { class: "v", text: mic.duplex || "—" })),
    el("div", { class: "m-row" },
      el("span", { text: "working / ready" }),
      el("span", { class: "v",
        text: counts.working + " / " + counts.listening }))));

  // agent cards ---------------------------------------------------------------
  for (const s of sessions) {
    const isMic = s.session_id === store.activeSession;
    const isSel = s.session_id === store.selectedAgent;
    const state = ctx.stateOf(s);
    const speaking = store.speaking && store.speaking.who === s.name
      ? store.speaking : null;
    const age = observedAge(s, state);
    // sel = the VIEWED agent (steel, like the tree); live = the mic
    // holder (amber). Both can be true; amber declares later and wins
    // the border.
    const card = el("div", { class: "card" + (isSel ? " sel" : "")
      + (isMic ? " live" : ""),
      title: `view ${s.name}'s work — the mic stays put`,
      onclick: () => ctx.focusAgent(s) });
    card.append(el("div", { class: "c-top" },
      el("span", { class: "dot " + state }),
      el("span", { class: "nm", text: s.name }),
      el("span", { class: "st " + (state === "working" || state === "listening"
        ? "w" : "") }, stateWord(state) + (age ? " · " + age : ""))));
    card.append(el("div", { class: "c-mid" },
      patchBtn(s, isMic, ctx),
      meter(isMic && (hearing || !!speaking))));
    // the caption line: live turn (holder) > speaking sentence > last say
    if (isMic && hearing) {
      card.append(el("div", { class: "c-cap live",
        text: store.ticker || "listening…" }));
    } else if (speaking) {
      card.append(el("div", { class: "c-cap live" },
        el("span", { text: "“" + (speaking.sentence || speaking.text || "") + "”" }),
        el("button", { class: "cap-more", text: "full",
          onclick: (e) => { e.stopPropagation(); ctx.onFull("agent"); } }),
        el("button", { class: "cmd-btn stop", text: "■", title: "stop speaking",
          onclick: async (e) => {
            e.stopPropagation();
            try { await ctx.command("interrupt", {}); }
            catch (err) { ctx.toast("stop: " + msg(err), true); }
          } })));
    } else if (isMic && store.lastRouted) {
      card.append(el("div", { class: "c-cap" },
        el("span", { text: "“" + store.lastRouted.text + "”" }),
        el("button", { class: "cap-more", text: "full",
          onclick: (e) => { e.stopPropagation(); ctx.onFull("you"); } })));
    } else {
      const say = lastSayOf(s);
      if (say) card.append(el("div", { class: "c-cap", text: "“" + say + "”" }));
    }
    const meta = el("div", { class: "c-meta" });
    if (s.queued) meta.append(el("span", { class: "q", text: "queue " + s.queued }));
    meta.append(el("span", { text: s.root
      ? String(s.root).split("/").slice(-2).join("/") : "session" }));
    card.append(meta);
    cards.append(card);
  }
  if (!sessions.length)
    cards.append(el("div", { class: "empty-note",
      text: "no agents — connect one and its card appears here" }));
}

const msg = (e) => (e instanceof Error ? e.message : String(e));
