// @ts-check
/**
 * Chat/ask dock panel (SPEC-WORKBENCH §4.3). Ask the workspace's primary
 * agent a question; the answer lands under the question card, rendered
 * with the shared (sanitizing) markdown renderer. Routing is honest: with
 * no review-capable agent attached the ask queues, and the panel says so.
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

/**
 * @param {HTMLElement} body
 * @param {any[]} asks oldest first
 * @param {{hasReviewAgent:boolean, onAsk:(text:string)=>void}} ctx
 */
export function renderChat(body, asks, ctx) {
  body.replaceChildren();
  const list = el("div", { class: "chat-list" });
  for (const a of asks) {
    const card = el("div", { class: "ask" },
      el("div", { class: "ask-text", text: a.text }));
    if (a.answer != null) {
      const answer = el("div", { class: "ask-answer" });
      renderMarkdown(answer, a.answer); // async fill; sanitized inside
      card.append(answer);
    } else {
      card.append(el("div", { class: "ask-pending", text: "waiting for the agent…" }));
    }
    list.append(card);
  }
  if (!asks.length)
    list.append(el("div", { class: "empty-note",
      text: "ask the workspace's agent — answers land here" }));
  body.append(list);
  if (!ctx.hasReviewAgent)
    body.append(el("div", { class: "chat-note",
      text: "no review-capable agent attached — asks deliver when one registers" }));

  const input = /** @type {HTMLTextAreaElement} */ (el("textarea", {
    class: "chat-input", rows: "2",
    placeholder: "ask the agent… (Enter sends, Shift+Enter for a newline)",
  }));
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      const text = input.value.trim();
      if (text) { ctx.onAsk(text); input.value = ""; }
    }
  });
  body.append(input);
  list.scrollTop = list.scrollHeight;
}
