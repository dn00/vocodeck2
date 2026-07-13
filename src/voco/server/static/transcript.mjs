// @ts-check
/**
 * The transcript (DESIGN-DECK U1) — the record half of voice presence,
 * scoped to the selected agent. A radio log, deliberately NOT a
 * messenger: flat entries (timestamp · speaker · wrapped text), one
 * shared column, no input of its own. "full" in the strip jumps here;
 * the entry being spoken karaoke-highlights via speech.sentence.
 * Data: session.transcript (both bounded logs), merged oldest-first.
 */

const el = (tag, attrs = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;
    else if (v != null) n.setAttribute(k, String(v));
  }
  for (const kid of kids) if (kid) n.append(kid);
  return n;
};

const fmtTime = (ts) =>
  new Date((ts || 0) * 1000).toLocaleTimeString([], { hour12: false });

/**
 * @param {HTMLElement} body
 * @param {?{inputs:any[], says:any[]}} data
 * @param {{agentName:?string, speaking:?{who:?string, sentence:?string, text:?string}}} ctx
 */
export function renderTranscript(body, data, ctx) {
  body.replaceChildren();
  const scroll = el("div", { class: "t-scroll", "aria-live": "polite" });
  body.append(scroll);
  if (!ctx.agentName) {
    scroll.append(el("div", { class: "empty-note",
      text: "select an agent — its conversation record lives here" }));
    return;
  }
  if (!data || (!data.inputs.length && !data.says.length)) {
    scroll.append(el("div", { class: "empty-note",
      text: "nothing said yet — talk, or type in the strip above" }));
    return;
  }
  // Merge both halves oldest-first; ties: user before agent.
  const lines = [
    ...data.inputs.map((x) => ({ ...x, kind: "you" })),
    ...data.says.map((x) => ({ ...x, kind: "agent" })),
  ].sort((a, b) => a.ts - b.ts || (a.kind === "you" ? -1 : 1));

  const speakingNow = ctx.speaking && ctx.speaking.who === ctx.agentName
    ? ctx.speaking : null;
  const lastAgentIdx = lines.map((line) => line.kind).lastIndexOf("agent");

  lines.forEach((line, i) => {
    const entry = el("div", { class: "tline " + line.kind,
      id: i === lines.length - 1 && line.kind === "you" ? "t-you-latest"
        : i === lastAgentIdx ? "t-agent-latest" : undefined });
    const head = el("div", { class: "thead" },
      el("b", { text: line.kind === "you" ? "you" : ctx.agentName }),
      el("span", { text: fmtTime(line.ts) }));
    if (line.kind === "you") {
      head.append(el("span", { text: line.origin || "voice" }));
      head.append(el("span", { text: "→ " + ctx.agentName }));
    }
    entry.append(head);
    // Karaoke: the entry being spoken splits around the current sentence.
    if (line.kind === "agent" && i === lastAgentIdx && speakingNow
        && speakingNow.sentence && line.text.includes(speakingNow.sentence)) {
      const at = line.text.indexOf(speakingNow.sentence);
      entry.append(el("div", { class: "ttext" },
        el("span", { class: "said", text: line.text.slice(0, at) }),
        el("span", { class: "saying", text: speakingNow.sentence }),
        el("span", { class: "tosay",
          text: line.text.slice(at + speakingNow.sentence.length) })));
    } else {
      entry.append(el("div", { class: "ttext", text: line.text }));
    }
    scroll.append(entry);
    if (line.kind === "you" && line.queued)
      scroll.append(el("div", { class: "tline meta",
        text: `queued — ${ctx.agentName} was mid-turn; delivered on their next listen` }));
  });
  scroll.scrollTop = scroll.scrollHeight;
}

/** Flash an entry after a "full" jump (toast-adjacent, self-clearing). */
export function flashEntry(body, target) {
  const id = target === "agent" ? "t-agent-latest" : "t-you-latest";
  const entry = body.querySelector("#" + id);
  if (!entry) return;
  entry.scrollIntoView({ block: "nearest" });
  entry.classList.remove("flash");
  // @ts-ignore restart the animation
  void entry.offsetWidth;
  entry.classList.add("flash");
  setTimeout(() => entry.classList.remove("flash"), 1700);
}
