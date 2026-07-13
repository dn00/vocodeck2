// @ts-check
/**
 * THE DECK (mk4, FL-push skin) — agents as cards laid left-to-right
 * along the bottom, like a real console's channel strips.
 *
 * The deck HEADER is the master strip (index7 port): the one input,
 * route display, transport (● talk · ■ stop · attention switch), mic
 * lock, duplex, working/ready counts, and the minimize toggle. Audio
 * lives on the instrument, not the ADE chrome above — and the header
 * survives minimize, so the transport stays reachable with the cards
 * folded. There is no master CARD: every card in the rack is an agent.
 *
 * MOUNT-ONCE HEADER: the deck re-renders on every voice event, so the
 * header (which holds the input) is built exactly once and updated in
 * place — the input's value and focus survive; only the cards strip
 * rebuilds per render (the presence.mjs pattern).
 *
 * Each card: a channel plate (state LED · module icon · NAME ·
 * state+age · ⊞ overview), the MIC patch + level meter, the agent's
 * LAST UTTERANCE (3 lines — the most informative element on a card),
 * and a meta row where the workspace path NEVER clips (the queue
 * step-cell cluster gives way first).
 *
 * Card body = VIEW the agent's work; only the MIC patch (or the tree's
 * agent row / a spoken phrase) moves the mic. ⊞ = view the agent's
 * overview without moving the mic (focusAgent).
 *
 * "listening" renders as "ready" (audit): the state means the agent is
 * parked at its listen call awaiting input — the WORD listening belongs
 * to the mic, not the agent.
 */

import { ic } from "./icons.mjs";

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

// The attention switch is direct-select (no cycle, so nothing to
// strand): label → daemon mode. WAKE disables itself when the server
// says the detector is unavailable (mic.state wake_available).
const ATTN_SEGS = /** @type {[string, string][]} */ ([
  ["MUTED", "muted"], ["PTT", "ptt_only"],
  ["WAKE", "wake"], ["ALWAYS", "always"],
]);
const HEARING = new Set(["capturing", "holding", "routing"]);
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

/** Latest render context — persistent header handlers read through
 * this, never through captured render-scope vars. */
let live = /** @type {any} */ (null);
/** Mount-once header slots (+ cards container). */
let head = /** @type {any} */ (null);

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

// Segmented LCD meter. With mic.level events flowing it reads the REAL
// input level (driven mode below); without them (older daemon) it falls
// back to a CSS pulse under .live. Never a random walk.
function meter(liveNow) {
  const m = el("span", { class: "meter" + (liveNow ? " live" : " idle") });
  for (let i = 0; i < 14; i++) m.append(el("i", { class: i > 10 ? "a" : "" }));
  return m;
}

/** The mic holder's mounted meter — updated out-of-band by setMicLevel
 * (a "miclevel" store notify), never through a panel re-render. */
let liveMeter = /** @type {?HTMLElement} */ (null);
let meterPeak = 0;
export function setMicLevel(level) {
  const m = liveMeter;
  if (!m || !m.isConnected) return;
  m.classList.add("driven"); // real signal available: pulse off
  const segs = m.children, n = segs.length;
  const lit = Math.round(level * n);
  // peak-hold decays per event; silence is the daemon's LAST event
  // (trailing zero), so it must clear the peak outright or it sticks
  meterPeak = level === 0 ? 0 : Math.max(meterPeak - 0.6, lit);
  for (let i = 0; i < n; i++) {
    segs[i].className = i < lit ? ("lit" + (i > n * 0.75 ? " hi" : ""))
      : (i === Math.round(meterPeak) && meterPeak > 0 ? "peak" : "");
  }
}

function lastSayOf(s) {
  const t = s.say_tail && s.say_tail.length
    ? s.say_tail[s.say_tail.length - 1] : null;
  return t && t.text ? t.text : null;
}

/** ADR-0003: every card keeps its patch (stable layout — captain). The
 * holder wears MIC; on others it's the explicit mover, which also
 * works while the mic is locked. */
function patchBtn(s, isMic, ctx) {
  if (isMic)
    return el("span", { class: "patch on", title: "your mic is patched here",
      text: "MIC" });
  return el("span", { class: "patch",
    title: `patch the mic to ${s.name}`
      + (ctx.micLocked() ? " (overrides the lock)" : ""),
    text: "mic",
    onclick: (e) => { e.stopPropagation();
      ctx.selectAgent(s, { force: true }); } });
}

// ---- the mount-once header --------------------------------------------------

function buildHead() {
  const count = el("span", { class: "caps", text: "DECK" });

  const input = /** @type {HTMLInputElement} */ (el("input", {
    type: "text", "aria-label": "type as speech" }));
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
  const route = el("span", { class: "droute",
    title: "who hears you — click an agent or a channel's mic patch to move it" });
  const dinput = el("div", { class: "dinput" },
    el("span", { class: "gt", text: ">" }), input, route);

  const talk = el("button", { class: "tbtn rec" },
    ic("rec", "ic s10"), el("span", { text: "talk" }));
  pttReleaseFn = () => {
    if (!pttHeld) return;
    pttHeld = false;
    talk.classList.remove("held");
    talk.setAttribute("aria-pressed", "false");
    live.ctx.command("ptt.release").catch(() => {});
  };
  talk.setAttribute("aria-pressed", "false");
  talk.addEventListener("pointerdown", (e) => {
    e.stopPropagation();
    if (!head.canPtt || pttHeld) return;
    pttHeld = true;
    talk.classList.add("held");
    talk.setAttribute("aria-pressed", "true");
    live.ctx.command("ptt.press").catch((err) => {
      live.ctx.toast("ptt: " + msg(err), true);
      pttHeld = false;
      talk.classList.remove("held");
      talk.setAttribute("aria-pressed", "false");
    });
  });

  const stop = el("button", { class: "tbtn stop",
    title: "interrupt: barge-in + Escape to the active agent",
    onclick: async () => {
      try { await live.ctx.command("interrupt", {}); }
      catch (e) { live.ctx.toast("interrupt: " + msg(e), true); }
    } }, ic("stop", "ic s10"));

  const segs = new Map();
  const sw = el("span", { class: "sw", role: "radiogroup" });
  for (const [label, mode] of ATTN_SEGS) {
    const seg = el("i", { text: label, role: "radio", tabindex: "0" });
    const pick = async () => {
      const mic = live.store.mic || {};
      if (!mic.attention) return; // headless: dead control, honest title
      if (mode === "wake" && mic.wake_available === false) {
        // disabled is still explainable: say what would enable it
        live.ctx.toast(
          "wake attention unavailable — set [audio].wake_model, then"
          + " restart voco-d");
        return;
      }
      try {
        const r = await live.ctx.command("mic.set", { attention: mode });
        if (r && r.refused) live.ctx.toast(r.refused, true);
      } catch (err) { live.ctx.toast("attention: " + msg(err), true); }
    };
    seg.addEventListener("click", (e) => { e.stopPropagation(); pick(); });
    seg.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); }
    });
    segs.set(mode, seg);
    sw.append(seg);
  }

  // ADR-0003 lock: pins the mic so agent clicks become view-only.
  // A bordered BUTTON, not a value — it must read as clickable.
  const lock = el("button", { class: "lockbtn", "aria-pressed": "false",
    onclick: (e) => { e.stopPropagation(); live.ctx.onToggleLock(); } });
  const duplex = el("button", { class: "dvx", onclick: async () => {
    const mic = live.store.mic || {};
    if (!mic.duplex) return; // headless: honest title, dead control
    const next = mic.duplex === "half_duplex" ? "full_duplex" : "half_duplex";
    try { await live.ctx.command("mic.set", { duplex: next }); }
    catch (err) { live.ctx.toast("duplex: " + msg(err), true); }
  } });
  const counts = el("span", { class: "lcd grn", title: "working / ready" });

  const tgl = el("button", { class: "tgl", onclick: () => {
    deckMin = !deckMin;
    localStorage.setItem("voco.deckMin", JSON.stringify(deckMin));
    if (lastArgs) renderRack(...lastArgs);
  } });

  const root = el("div", { class: "dhead" },
    count, dinput,
    el("div", { class: "dtransport" }, talk, stop, sw),
    lock, duplex, counts, tgl);
  return { root, count, input, route, talk, sw, segs, lock, duplex, counts,
    tgl, canPtt: false };
}

function updateHead(store, ctx, sessions) {
  const mic = store.mic || {};
  head.count.textContent = "DECK · " + sessions.length;

  // the ONE input: placeholder/disabled only — value and focus SURVIVE.
  // No voice loop = no "say" (audit #7: the placeholder must not offer
  // a voice path a headless daemon cannot hear).
  head.input.placeholder = mic.attention
    ? "say “deck …” or type" : "type — routes like speech";
  head.input.disabled = !store.connected;
  const activeName = store._nameOf(store.activeSession);
  head.route.textContent = activeName
    ? "ROUTE → " + String(activeName).toUpperCase() : "ROUTE → —";
  head.route.classList.toggle("none", !activeName);

  head.canPtt = !!mic.attention && mic.attention !== "muted";
  head.talk.classList.toggle("none", !head.canPtt);
  head.talk.title = head.canPtt
    ? "hold to open the mic (Space works too, in ptt_only)"
    : "needs a voice loop, not muted";
  if (!pttHeld) head.talk.classList.remove("held");

  const headless = !mic.attention;
  head.sw.classList.toggle("none", headless);
  head.sw.title = headless
    ? "no voice loop (daemon started without audio)"
    : "attention — who the mic listens for";
  for (const [mode, seg] of head.segs) {
    const on = !headless && mic.attention === mode;
    seg.classList.toggle("on", on);
    seg.setAttribute("aria-checked", String(on));
    if (mode === "wake") {
      const off = mic.wake_available === false; // strict: old servers keep it
      seg.classList.toggle("off", off);
      seg.setAttribute("aria-disabled", String(off));
      seg.title = off ? "wake unavailable — no detector loaded" : "";
    }
  }

  const locked = ctx.micLocked();
  head.lock.classList.toggle("on", locked);
  head.lock.setAttribute("aria-pressed", String(locked));
  head.lock.title = locked
    ? "mic is pinned — agent clicks are view-only; click to unlock"
    : "mic follows your selection; click to pin it in place";
  head.lock.replaceChildren(ic("lock", "ic s9"),
    el("span", { text: locked ? "locked" : "follows selection" }));

  head.duplex.textContent = mic.duplex
    ? String(mic.duplex).replace("_duplex", "") : "—";
  head.duplex.title = mic.duplex
    ? `duplex: ${mic.duplex} — click to switch to `
      + (mic.duplex === "half_duplex" ? "full_duplex" : "half_duplex")
    : "duplex — no voice loop";

  const counts = { working: 0, listening: 0 };
  for (const s of store.sessions.values()) {
    const st = ctx.stateOf(s);
    if (st in counts) counts[st]++;
  }
  head.counts.textContent = counts.working + " / " + counts.listening;

  head.tgl.textContent = deckMin ? "▢" : "—";
  head.tgl.title = deckMin ? "expand the deck" : "minimize the deck";
}

/**
 * @param {HTMLElement} deck
 * @param {import("./store.mjs").Store} store
 * @param {{command:(cmd:string, payload?:object)=>Promise<any>,
 *   selectAgent:(s:any, opts?:{force?:boolean})=>void,
 *   focusAgent:(s:any)=>void, stateOf:(s:any)=>string,
 *   micLocked:()=>boolean, onToggleLock:()=>void,
 *   onFull:(target:"you"|"agent")=>void,
 *   toast:(msg:string, sticky?:boolean)=>void}} ctx
 */
export function renderRack(deck, store, ctx) {
  lastArgs = [deck, store, ctx];
  live = { store, ctx };
  // STABLE order (captain): a console's channels never move — spatial
  // memory beats attention-sorting here. Mic/blocked speak via color.
  const sessions = [...store.sessions.values()]
    .sort((a, b) => a.name.localeCompare(b.name));

  if (!head || !deck.contains(head.root)) {
    deck.replaceChildren();
    head = buildHead();
    head.cards = el("div", { class: "cards" });
    deck.append(head.root, head.cards);
  }
  deck.classList.toggle("min", deckMin);
  updateHead(store, ctx, sessions);

  const cards = head.cards;
  cards.replaceChildren();
  const hearing = HEARING.has(store.turnState);

  if (deckMin) {
    for (const s of sessions) {
      const isMic = s.session_id === store.activeSession;
      const isSel = s.session_id === store.selectedAgent;
      const state = ctx.stateOf(s);
      cards.append(el("span", {
        class: "chipcard" + (isMic ? " live" : "") + (isSel ? " sel" : ""),
        title: ctx.micLocked()
          ? `view ${s.name}'s work (mic locked)`
          : `talk to ${s.name} — selection is routing`,
        onclick: () => ctx.selectAgent(s) },
        ic("module", "ic s10 mstate " + state),
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

  // agent cards — channels only; the master strip is the header --------------
  for (const s of sessions) {
    const isMic = s.session_id === store.activeSession;
    const isSel = s.session_id === store.selectedAgent;
    const state = ctx.stateOf(s);
    const speaking = store.speaking && store.speaking.who === s.name
      ? store.speaking : null;
    const age = observedAge(s, state);
    // sel = the VIEWED agent (steel); live = the mic holder (amber,
    // declared later, wins the border). Unlocked they usually coincide;
    // the dual language appears exactly in the locked/split state.
    const card = el("div", { class: "card" + (isSel ? " sel" : "")
      + (isMic ? " live" : ""),
      title: ctx.micLocked()
        ? `view ${s.name}'s work (mic locked)`
        : `talk to ${s.name} — selection is routing`,
      onclick: () => ctx.selectAgent(s) });
    // the channel plate: raised header strip, like an FL channel button.
    // The module icon IS the state LED (one signal, not two glyphs); the
    // state word stays adjacent per the design system.
    card.append(el("div", { class: "plate" },
      ic("module", "ic mstate " + state),
      el("span", { class: "nm", text: s.name }),
      el("span", { class: "st " + (state === "working" || state === "listening"
        ? "w" : "") }, stateWord(state) + (age ? " · " + age : "")),
      el("button", { class: "pbtn", title: `open ${s.name}'s overview`,
        onclick: (e) => { e.stopPropagation(); ctx.focusAgent(s); } },
        ic("overview"))));
    const body = el("div", { class: "cardbody" });
    card.append(body);
    const mtr = meter(isMic && (hearing || !!speaking));
    if (isMic) liveMeter = mtr; // the slot setMicLevel drives out-of-band
    body.append(el("div", { class: "c-mid" },
      patchBtn(s, isMic, ctx),
      mtr));
    // the caption is the AGENT's voice only: live turn (holder) >
    // speaking sentence > last say. YOUR routed text belongs to the
    // transcript, never to an agent's channel strip.
    if (isMic && hearing) {
      body.append(el("div", { class: "c-cap live",
        text: store.ticker || "listening…" }));
    } else if (speaking) {
      body.append(el("div", { class: "c-cap live" },
        el("span", { text: "“" + (speaking.sentence || speaking.text || "") + "”" }),
        el("button", { class: "cap-more", text: "full",
          onclick: (e) => { e.stopPropagation(); ctx.onFull("agent"); } }),
        el("button", { class: "cmd-btn stop", text: "■", title: "stop speaking",
          onclick: async (e) => {
            e.stopPropagation();
            try { await ctx.command("interrupt", {}); }
            catch (err) { ctx.toast("stop: " + msg(err), true); }
          } })));
    } else {
      const say = lastSayOf(s);
      body.append(say
        ? el("div", { class: "c-cap", text: "“" + say + "”" })
        // a card with no history reads intentional, not broken
        : el("div", { class: "c-cap none", text: "no utterances yet" }));
    }
    // meta: the path NEVER clips; the queue step cluster gives way
    const meta = el("div", { class: "c-meta" });
    if (s.queued) {
      const steps = el("span", { class: "steps" });
      for (let i = 0; i < 4; i++)
        steps.append(el("i", { class: i < Math.min(s.queued, 4) ? "on" : "" }));
      meta.append(el("span", { class: "qgrp" },
        el("span", { text: "queue" }), steps,
        el("span", { class: "q", text: String(s.queued) })));
    }
    meta.append(el("span", { class: "path", text: s.root
      ? String(s.root).split("/").slice(-2).join("/") : "session" }));
    body.append(meta);
    cards.append(card);
  }
  if (!sessions.length)
    cards.append(el("div", { class: "empty-note",
      text: "no agents — connect one and its card appears here" }));
}

const msg = (e) => (e instanceof Error ? e.message : String(e));
