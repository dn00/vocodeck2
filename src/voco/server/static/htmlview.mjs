// @ts-check
/**
 * HTML artifact view (B1b) — the reference's html sections on voco's
 * seams. Served artifacts render in a HARD-sandboxed iframe
 * (sandbox="allow-scripts" only — opaque origin: artifact JS can never
 * touch the deck, its token, or the API; the served document also
 * carries a CSP sandbox header, so even a top-level open stays caged).
 * The injected shim speaks postMessage: Annotate toggle → click an
 * element → {selector, exact, tag} becomes an `element` finding.
 * url-mode artifacts iframe a dev server directly — un-shimmed, so
 * Annotate is honestly absent; review by looking.
 *
 * Deep links (da:…) from inside the artifact arrive as da-nav messages
 * and are routed by the caller (ctx.onNav) — an agent-pushed dashboard
 * can say "3 findings in export.ts" and make it one click.
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

/**
 * @param {HTMLElement} view
 * @param {{mode:string, src:string, params?:{annotatable?:boolean}}} content
 * @param {{title:string, wb:string, reveal?:?string,
 *   onAnnotate:(anchor:object, text:string, kind:string, blocking:boolean)=>void,
 *   onNav:(target:string)=>void}} ctx
 * @returns {() => void} cleanup (removes the message listener)
 */
export function renderHtmlView(view, content, ctx) {
  view.replaceChildren();
  const served = content.mode === "artifact";
  const annotatable = served
    && !(content.params && content.params.annotatable === false);
  const src = served
    ? content.src + "&wb=" + encodeURIComponent(ctx.wb)
    : content.src;

  const iframe = /** @type {HTMLIFrameElement} */ (el("iframe", {
    class: "artifact-frame",
    // allow-scripts ONLY — no allow-same-origin: the artifact runs with
    // an opaque origin and cannot reach the deck or the API.
    sandbox: "allow-scripts",
    src,
    title: ctx.title,
  }));

  let annotating = false;
  const bar = el("div", { class: "artifact-bar" });
  if (annotatable) {
    const btn = el("button", { class: "whbtn",
      title: "click an element inside the render to annotate it",
      onclick: () => {
        annotating = !annotating;
        btn.classList.toggle("on", annotating);
        post({ type: "da-annotate-mode", on: annotating });
      } }, "annotate");
    bar.append(btn);
  } else {
    bar.append(el("span", { class: "micro",
      text: served ? "read-only artifact" : "live url — view only, un-shimmed" }));
  }
  bar.append(el("span", { class: "micro", text: ctx.title }));

  const editorSlot = el("div", {});
  view.append(bar, editorSlot, iframe);

  function post(msg) {
    if (iframe.contentWindow) iframe.contentWindow.postMessage(msg, "*");
  }

  function openEditor(picked) {
    editorSlot.replaceChildren();
    let kind = "concern";
    const excerpt = picked.exact.length > 60
      ? picked.exact.slice(0, 57) + "…" : picked.exact;
    const target = el("div", { class: "editor-target" });
    const setTarget = () => (target.textContent =
      `${kind} on <${picked.tag}>: “${excerpt}”`);
    setTarget();
    const ta = /** @type {HTMLTextAreaElement} */ (el("textarea", {
      placeholder: "describe the concern with this element…" }));
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
    const close = () => editorSlot.replaceChildren();
    const commit = () => {
      const text = ta.value.trim();
      if (!text) { ta.focus(); return; }
      ctx.onAnnotate(
        { kind: "element", selector: picked.selector, exact: picked.exact,
          tag: picked.tag },
        text, kind, blocking.checked);
      close();
    };
    ta.addEventListener("keydown", (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); commit(); }
      if (e.key === "Escape") { e.preventDefault(); close(); }
    });
    editorSlot.append(el("div", { class: "doc-editor annot-editor" },
      target, ta, pills,
      el("div", { class: "editor-actions" },
        el("button", { class: "tbtn primary", text: "add annotation", onclick: commit }),
        el("button", { class: "tbtn", text: "cancel", onclick: close })),
      el("div", { class: "flow-note",
        text: "the element's css path is the anchor; its text is the evidence" })));
    ta.focus();
  }

  function onMessage(ev) {
    // only OUR iframe's messages count (any window can postMessage)
    if (ev.source !== iframe.contentWindow) return;
    const d = ev.data || {};
    if (d.type === "da-element-picked" && typeof d.selector === "string")
      openEditor({ selector: d.selector,
        exact: String(d.exact || ""), tag: String(d.tag || "?") });
    if (d.type === "da-nav" && typeof d.target === "string")
      ctx.onNav(d.target);
  }
  window.addEventListener("message", onMessage);

  if (ctx.reveal) {
    iframe.addEventListener("load", () =>
      post({ type: "da-reveal", selector: ctx.reveal }));
  }

  return () => window.removeEventListener("message", onMessage);
}
