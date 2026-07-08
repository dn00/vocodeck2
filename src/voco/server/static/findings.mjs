// @ts-check
/**
 * Annotations dock (SPEC-WORKBENCH §4; DESIGN-DECK rev 4.1 fitem look).
 * Flat rows: status/kind tags + location + ✕ withdraw (undoable via
 * toast — the caller owns the undo), text, and the agent's reply as an
 * inset quote. Click the location to reveal the diff line.
 */

import { renderMarkdown } from "./markdown.mjs";

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

const STATUS_TAG = {
  open: ["open", "open"],
  addressed: ["done", "addressed"],
  disputed: ["q", "disputed"],
  "wont-fix": ["done", "won't fix"],
};

/**
 * @param {HTMLElement} body
 * @param {any[]} findings
 * @param {{onWithdraw:(id:string)=>void, onReveal:(f:any)=>void,
 *   pageRev?:(pageId:string)=>?number, emptyText?:string,
 *   restoreScroll?:number}} ctx
 */
export function renderFindings(body, findings, ctx) {
  body.replaceChildren();
  const live = findings.filter((f) => f.status !== "withdrawn");
  if (!live.length) {
    body.append(el("div", { class: "empty-note",
      text: ctx.emptyText || "no annotations — click a diff line to flag one" }));
    return;
  }
  const scroll = el("div", { class: "dock-scroll" });
  for (const f of live) {
    const a = f.anchor || {};
    // diff anchors: file:line; text anchors (docs, B1a): the quote
    const loc = a.file
      ? `${a.file.split("/").pop()}:${a.startLine}${a.endLine > a.startLine ? "–" + a.endLine : ""}`
      : a.exact
        ? `“${String(a.exact).slice(0, 28)}${String(a.exact).length > 28 ? "…" : ""}”`
        : "—";
    const [tagCls, tagLabel] = STATUS_TAG[f.status] || ["open", f.status];
    const rev = ctx.pageRev ? ctx.pageRev(f.page_id) : null;
    const stale = rev != null && f.rev < rev;
    const head = el("div", { class: "fhead" },
      el("span", { class: "tag " + tagCls, text: tagLabel }),
      f.kind === "question" ? el("span", { class: "tag q", text: "question" }) : null,
      f.kind === "nit" ? el("span", { class: "tag", text: "nit" }) : null,
      f.blocking ? el("span", { class: "tag blocking", text: "blocking" }) : null,
      stale ? el("span", { class: "tag stale", text: `stale r${f.rev}` }) : null,
      el("span", { class: "loc", text: loc, onclick: () => ctx.onReveal(f) }),
      el("button", { class: "fx", text: "✕", title: "withdraw — undoable via toast",
        onclick: () => ctx.onWithdraw(f.finding_id) }));
    const item = el("div", { class: "fitem" }, head,
      el("div", { class: "ftext", text: f.text }));
    if (f.note)
      item.append(el("div", { class: "freply" },
        el("b", { text: "agent" }), " — " + f.note,
        f.commit ? el("span", { class: "micro mono", text: " " + f.commit }) : null));
    if (f.answer) {
      // Agent replies are markdown (§4.3) — shared sanitizing renderer.
      const reply = el("div", { class: "freply" }, el("b", { text: "agent" }), " — ");
      const md = el("span", {});
      renderMarkdown(md, f.answer);
      reply.append(md);
      item.append(reply);
    }
    scroll.append(item);
  }
  body.append(scroll);
  if (ctx.restoreScroll) scroll.scrollTop = ctx.restoreScroll;
}
