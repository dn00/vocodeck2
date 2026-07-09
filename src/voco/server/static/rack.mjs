// @ts-check
/**
 * The channel rack (CONSOLE mk3, M5; BUILD-CONSOLE.md) — agents as
 * channel strips on the right edge. Your mic is a signal you patch:
 * the amber MIC button always shows where your words go, and clicking
 * a patch is exactly the tree's agent-click (the ONLY mic mover).
 *
 * The LIVE channel (mic holder) runs the machinery: level meter
 * (animates while the turn is capturing/holding/routing), the live
 * caption (listening ticker / last routed utterance), and — when an
 * agent is speaking aloud — the speaking line with ■ stop. Idle
 * channels fold to two lines. The master block at the foot carries
 * duplex, attention (click-cycles — the old orb's function), and the
 * working/listening counts.
 *
 * Ages are OBSERVED, not fabricated: a channel shows "· Ns" only once
 * this client has seen its state change (no state-change timestamp
 * rides the protocol today).
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

// session_id -> {state, since} — observed transitions only.
const seen = new Map();
function observedAge(s, state) {
  const rec = seen.get(s.session_id);
  if (!rec || rec.state !== state) {
    seen.set(s.session_id, { state, since: Date.now() });
    return rec ? "0s" : null; // fresh transition; unknown on first sight
  }
  const secs = Math.floor((Date.now() - rec.since) / 1000);
  if (secs < 5) return null;
  if (secs < 120) return secs + "s";
  if (secs < 7200) return Math.floor(secs / 60) + "m";
  return Math.floor(secs / 3600) + "h";
}

function meter(liveLevel) {
  const m = el("span", { class: "meter" + (liveLevel ? " live" : "") });
  for (let i = 0; i < 6; i++)
    m.append(el("i", { class: i > 3 ? "a" : "" }));
  return m;
}

/**
 * @param {HTMLElement} rack
 * @param {import("./store.mjs").Store} store
 * @param {{command:(cmd:string, payload?:object)=>Promise<any>,
 *   selectAgent:(s:any)=>void, stateOf:(s:any)=>string,
 *   onFull:(target:"you"|"agent")=>void,
 *   toast:(msg:string, sticky?:boolean)=>void}} ctx
 */
export function renderRack(rack, store, ctx) {
  rack.replaceChildren();
  rack.append(el("div", { class: "rhead caps", text: "CHANNELS — MIC PATCHES HERE" }));
  const sessions = [...store.sessions.values()]
    .sort((a, b) => (a.session_id === store.activeSession ? -1 : 1)
      - (b.session_id === store.activeSession ? -1 : 1)
      || a.name.localeCompare(b.name));
  const hearing = HEARING.has(store.turnState);

  for (const s of sessions) {
    const isMic = s.session_id === store.activeSession;
    const state = ctx.stateOf(s);
    const speaking = store.speaking && store.speaking.who === s.name
      ? store.speaking : null;
    const age = observedAge(s, state);
    const chan = el("div", { class: "chan" + (isMic ? " live" : ""),
      title: `talk to ${s.name} (moves the mic)`,
      onclick: () => ctx.selectAgent(s) });
    chan.append(el("div", { class: "ch-top" },
      el("span", { class: "dot " + state }),
      el("span", { class: "nm", text: s.name }),
      el("span", { class: "st " + (state === "working" || state === "listening"
        ? "w" : "") }, state + (age ? " · " + age : ""))));
    const patch = el("span", { class: "patch" + (isMic ? " on" : ""),
      title: isMic ? "your mic is patched here"
        : `patch the mic to ${s.name}`,
      text: isMic ? "MIC" : "mic",
      onclick: (e) => { e.stopPropagation(); if (!isMic) ctx.selectAgent(s); } });
    if (isMic) {
      const mic = el("div", { class: "ch-mic" }, patch,
        meter(hearing || !!speaking));
      chan.append(mic);
      // live caption: listening ticker beats last-routed
      if (hearing) {
        chan.append(el("div", { class: "ch-cap", text:
          store.ticker || "listening…" }));
      } else if (store.lastRouted) {
        const cap = el("div", { class: "ch-cap" },
          el("span", { text: "“" + store.lastRouted.text + "”" }),
          el("button", { class: "cap-more", text: "full",
            onclick: (e) => { e.stopPropagation(); ctx.onFull("you"); } }));
        chan.append(cap);
      }
    } else {
      chan.append(el("div", { class: "ch-idle" }, patch,
        el("span", {},
          s.queued ? el("span", { class: "q", text: "queue " + s.queued + " · " }) : "",
          el("span", { text: (s.capabilities || []).length ? "session" : "agent" }))));
    }
    // speaking line rides the speaker's channel, wherever the mic is
    if (speaking) {
      chan.append(el("div", { class: "ch-speak" },
        el("span", { class: "eq" }, el("i"), el("i"), el("i")),
        el("span", { class: "sp-text",
          text: "“" + (speaking.sentence || speaking.text || "") + "”" }),
        el("button", { class: "cap-more", text: "full",
          onclick: (e) => { e.stopPropagation(); ctx.onFull("agent"); } }),
        el("button", { class: "cmd-btn stop", text: "■", title: "stop speaking",
          onclick: async (e) => {
            e.stopPropagation();
            try { await ctx.command("interrupt", {}); }
            catch (err) { ctx.toast("stop: " + msg(err), true); }
          } })));
    }
    rack.append(chan);
  }
  if (!sessions.length)
    rack.append(el("div", { class: "empty-note",
      text: "no agents — connect one and its channel appears here" }));

  // master block ---------------------------------------------------------------
  const mic = store.mic || {};
  const counts = { working: 0, listening: 0 };
  for (const s of store.sessions.values()) {
    const st = ctx.stateOf(s);
    if (st in counts) counts[st]++;
  }
  const attn = el("span", { class: "v hot",
    text: mic.attention || "headless" });
  if (mic.attention) {
    attn.classList.add("cyc");
    attn.title = `attention: ${mic.attention} — click cycles muted → wake → always`;
    attn.addEventListener("click", async () => {
      const next = ATTENTION_CYCLE[
        (ATTENTION_CYCLE.indexOf(mic.attention) + 1) % ATTENTION_CYCLE.length];
      try { await ctx.command("mic.set", { attention: next }); }
      catch (e) { ctx.toast("attention: " + msg(e), true); }
    });
  } else {
    attn.title = "no voice loop (daemon started without audio)";
    attn.classList.remove("hot");
  }
  rack.append(el("div", { class: "master" },
    el("div", { class: "row" }, el("span", { text: "duplex" }),
      el("span", { class: "v", text: mic.duplex || "—" })),
    el("div", { class: "row" }, el("span", { text: "attention" }), attn),
    el("div", { class: "row" }, el("span", { text: "working / listening" }),
      el("span", { class: "v",
        text: counts.working + " / " + counts.listening }))));
}

const msg = (e) => (e instanceof Error ? e.message : String(e));
