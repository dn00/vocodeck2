// @ts-check
/**
 * Findings dock (SPEC-WORKBENCH §4). Lists the selected workspace's findings
 * with kind, status chip, blocking flag, anchor, and agent note/answer. Click
 * a card to reveal its diff line; withdraw removes it (status → withdrawn).
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

const STATUS_LABEL = {
  open: "open", addressed: "addressed", disputed: "disputed",
  "wont-fix": "won't fix", withdrawn: "withdrawn",
};

/**
 * @param {HTMLElement} body
 * @param {any[]} findings
 * @param {{onWithdraw:(id:string)=>void, onReveal:(f:any)=>void}} ctx
 */
export function renderFindings(body, findings, ctx) {
  body.replaceChildren();
  const live = findings.filter((f) => f.status !== "withdrawn");
  if (!live.length) {
    body.append(el("div", { class: "empty-note",
      text: "no findings — click a diff line to flag one" }));
    return;
  }
  for (const f of live) {
    const a = f.anchor || {};
    const loc = a.file
      ? `${a.file}:${a.startLine}${a.endLine > a.startLine ? "–" + a.endLine : ""}`
      : "—";
    const card = el("div", { class: "finding kind-" + f.kind });
    card.append(
      el("div", { class: "finding-head" },
        el("span", { class: "finding-kind", text: f.kind }),
        f.blocking ? el("span", { class: "finding-blocking", text: "blocking" }) : null,
        el("span", { class: "finding-status s-" + f.status,
          text: STATUS_LABEL[f.status] || f.status })),
      el("div", { class: "finding-loc", text: loc, onclick: () => ctx.onReveal(f) }),
      el("div", { class: "finding-text", text: f.text }));
    if (f.note)
      card.append(el("div", { class: "finding-note", text: "note: " + f.note }));
    if (f.answer)
      card.append(el("div", { class: "finding-answer", text: f.answer }));
    card.append(el("div", { class: "finding-actions" },
      el("button", { class: "btn ghost sm", text: "withdraw",
        onclick: () => ctx.onWithdraw(f.finding_id) })));
    body.append(card);
  }
}
