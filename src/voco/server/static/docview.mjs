// @ts-check
/**
 * Annotatable doc view (B1a) — the reference's doc-review surface on
 * voco's seams. Renders a doc page's markdown as selectable prose;
 * SELECT text or plain-CLICK a block (paragraph, list item, heading,
 * code fence, quote, table cell) to annotate it. Anchors are the
 * reference's SARIF-style text ranges — {kind:"text", exact, prefix,
 * suffix, start, end} — so findings re-anchor after edits and go
 * stale-not-dropped on re-push.
 *
 * PORTED INVARIANTS (from doc-review-panel.mjs — the paid-for lessons):
 * - offsets are measured against THE PROSE ELEMENT ONLY;
 * - the editor is inserted as a SIBLING of the prose (never inside it)
 *   and floated at the anchor, so an open editor never shifts a later
 *   selection's offsets;
 * - an active text selection WINS over the block click;
 * - links keep navigating (a click on <a> never opens an editor);
 * - CONTEXT_CHARS=40 of prefix/suffix ride along for re-anchoring;
 * - ctrl/cmd+enter commits, Esc cancels, empty text never commits.
 */

import { renderMarkdown } from "./markdown.mjs";
import { findByText, flash } from "./reveal.mjs";

const CONTEXT_CHARS = 40;
const BLOCKS = "p,li,h1,h2,h3,h4,h5,h6,pre,blockquote,td,th";

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

/** Absolute character offset of (node, nodeOffset) within root's text. */
function offsetWithin(root, node, nodeOffset) {
  const r = document.createRange();
  r.selectNodeContents(root);
  try { r.setEnd(node, nodeOffset); } catch (_) { return 0; }
  return r.toString().length;
}

/**
 * @param {HTMLElement} view
 * @param {string} markdown
 * @param {{title:string, readOnly?:boolean, reveal?:?string,
 *   onAnnotate:(anchor:object, text:string, kind:string, blocking:boolean)=>void}} ctx
 */
export async function renderDocView(view, markdown, ctx) {
  view.replaceChildren();
  const doc = el("div", { class: "review-doc" + (ctx.readOnly ? " no-annotate" : "") });
  const prose = el("div", { class: "review-prose md" });
  doc.append(prose);
  view.append(doc);
  await renderMarkdown(prose, markdown || "");
  if (!view.contains(prose)) return; // a newer render replaced us mid-await

  if (ctx.reveal) {
    // Fall back to flashing the whole doc: a reveal click must always
    // land SOMEWHERE visible, even when the quote has drifted away.
    const hit = findByText(prose, ctx.reveal) || doc;
    requestAnimationFrame(() => flash(hit, view));
  }
  if (ctx.readOnly) return;

  function removeEditor() {
    const ex = doc.querySelector(".doc-editor");
    if (ex) ex.remove();
  }

  function anchorFrom(exact, start, end) {
    const full = prose.textContent || "";
    return {
      kind: "text",
      exact,
      prefix: full.slice(Math.max(0, start - CONTEXT_CHARS), start),
      suffix: full.slice(end, end + CONTEXT_CHARS),
      start,
      end,
    };
  }

  /** Selection → anchor, or null when collapsed/outside the prose. */
  function resolveSelection() {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || sel.rangeCount === 0) return null;
    const range = sel.getRangeAt(0);
    const exact = sel.toString();
    if (!exact.trim()) return null;
    let node = range.commonAncestorContainer;
    if (node.nodeType === Node.TEXT_NODE) node = /** @type {any} */ (node).parentNode;
    const inProse = node && /** @type {Element} */ (node).closest
      ? /** @type {Element} */ (node).closest(".review-prose") : null;
    if (inProse !== prose) return null;
    if (!prose.contains(range.startContainer) || !prose.contains(range.endContainer))
      return null;
    const start = offsetWithin(prose, range.startContainer, range.startOffset);
    const end = offsetWithin(prose, range.endContainer, range.endOffset);
    return { anchor: anchorFrom(exact, start, end), exact,
      rect: range.getBoundingClientRect() };
  }

  /** Plain click on a block annotates the whole block (per-line ask). */
  function resolveBlock(e) {
    if (!e.target.closest || e.target.closest("a")) return null; // links navigate
    if (!prose.contains(e.target)) return null;
    const block = e.target.closest(BLOCKS);
    if (!block || !prose.contains(block)) return null;
    const exact = block.textContent || "";
    if (!exact.trim()) return null;
    const start = offsetWithin(prose, block, 0);
    const end = start + exact.length;
    return { anchor: anchorFrom(exact, start, end), exact,
      rect: block.getBoundingClientRect() };
  }

  function openEditor(hit) {
    removeEditor();
    let kind = "concern";
    const excerpt = hit.exact.length > 80 ? hit.exact.slice(0, 77) + "…" : hit.exact;
    const target = el("div", { class: "editor-target" });
    const setTarget = () =>
      (target.textContent = `${kind} on ${ctx.title}: “${excerpt}”`);
    setTarget();
    const ta = /** @type {HTMLTextAreaElement} */ (el("textarea", {
      placeholder: "describe the concern with this passage…" }));
    const pills = el("div", { class: "finding-controls" });
    const pillEls = [];
    for (const k of ["concern", "question", "nit"]) {
      const p = el("button", { class: "fpill" + (k === kind ? " active" : ""),
        text: k,
        onclick: () => {
          kind = k;
          for (const q of pillEls) q.classList.toggle("active", q.textContent === k);
          setTarget();
        } });
      pillEls.push(p);
      pills.append(p);
    }
    const blocking = /** @type {HTMLInputElement} */ (el("input", { type: "checkbox" }));
    pills.append(el("label", { class: "fblock" }, blocking, "blocking"));
    const commit = () => {
      const text = ta.value.trim();
      if (!text) { ta.focus(); return; }
      ctx.onAnnotate(hit.anchor, text, kind, blocking.checked);
      removeEditor();
      const s = window.getSelection();
      if (s && s.removeAllRanges) s.removeAllRanges();
    };
    ta.addEventListener("keydown", (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); commit(); }
      if (e.key === "Escape") { e.preventDefault(); removeEditor(); }
    });
    const box = el("div", { class: "doc-editor annot-editor" },
      target, ta, pills,
      el("div", { class: "editor-actions" },
        el("button", { class: "tbtn primary", text: "add annotation", onclick: commit }),
        el("button", { class: "tbtn", text: "cancel", onclick: removeEditor })),
      el("div", { class: "flow-note",
        text: "tip: select a passage or just click a block. ctrl/cmd+enter to add." }));
    // SIBLING of the prose, floated at the anchor — never inside it
    // (an editor inside the prose would shift later selections' offsets).
    doc.insertBefore(box, prose.nextSibling);
    if (hit.rect && hit.rect.height >= 0) {
      const dr = doc.getBoundingClientRect();
      box.classList.add("floating");
      box.style.top = Math.max(0, hit.rect.bottom - dr.top + 6) + "px";
    }
    ta.focus();
  }

  doc.addEventListener("mouseup", (e) => {
    if (e.target.closest && e.target.closest(".doc-editor")) return;
    const sel = resolveSelection(); // an active selection WINS
    if (sel) { openEditor(sel); return; }
    const block = resolveBlock(e);
    if (block) openEditor(block);
  });
}
