// Artifact annotator shim (B1b) — ported kernel from the diff-annotate
// reference (artifact-annotator.js). Injected by /v1/artifact/{page_id}
// into every SERVED html artifact, inside the sandboxed iframe. Plain
// script on purpose — it must run in arbitrary artifact documents. It
// does nothing until the parent view turns annotate mode on:
//   parent → iframe:  {type:'da-annotate-mode', on:bool}
//   iframe → parent:  {type:'da-element-picked', selector, exact, tag}
//   iframe → parent:  {type:'da-nav', target:'da:…'}   (deep links)
//   parent → iframe:  {type:'da-reveal', selector}     (jump-back pulse)
// The selector is a CSS path (id and nth-of-type hops) — the element's
// IDENTITY; `exact` is its trimmed text content — the EVIDENCE.
(function () {
  if (window.__vocoAnnotator) return;
  window.__vocoAnnotator = true;

  var mode = false;
  var marked = null; // { el, prev }

  function clearMark() {
    if (marked) {
      try { marked.el.style.outline = marked.prev; } catch (e) { /* gone */ }
      marked = null;
    }
  }

  function esc(s) {
    try { return CSS.escape(s); } catch (e) { return s.replace(/[^a-zA-Z0-9_-]/g, "\\$&"); }
  }

  function cssPath(el) {
    var parts = [];
    var node = el;
    while (node && node.nodeType === 1 && node !== document.body && node !== document.documentElement) {
      if (node.id) { parts.unshift("#" + esc(node.id)); break; }
      var tag = node.tagName.toLowerCase();
      var nth = 1;
      var sib = node.previousElementSibling;
      while (sib) { if (sib.tagName === node.tagName) nth++; sib = sib.previousElementSibling; }
      parts.unshift(tag + ":nth-of-type(" + nth + ")");
      node = node.parentElement;
    }
    return parts.join(" > ") || el.tagName.toLowerCase();
  }

  window.addEventListener("message", function (ev) {
    var d = ev.data || {};
    if (d.type === "da-annotate-mode") {
      mode = !!d.on;
      document.documentElement.style.cursor = mode ? "crosshair" : "";
      if (!mode) clearMark();
    }
    if (d.type === "da-reveal" && d.selector) {
      try {
        var el = document.querySelector(d.selector);
        if (el) {
          el.scrollIntoView({ block: "center", behavior: "smooth" });
          var prev = el.style.outline;
          el.style.outline = "3px solid #e0a458";
          setTimeout(function () { el.style.outline = prev; }, 1600);
        }
      } catch (e) { /* stale selector */ }
    }
  });

  document.addEventListener("mouseover", function (ev) {
    if (!mode) return;
    clearMark();
    var el = ev.target;
    if (!el || el.nodeType !== 1) return;
    marked = { el: el, prev: el.style.outline };
    el.style.outline = "2px solid #63a8f2";
  }, true);

  document.addEventListener("click", function (ev) {
    if (!mode) {
      // Deep links INTO the workspace: href="da:…" routes in the parent;
      // plain links still navigate.
      var link = ev.target && ev.target.closest ? ev.target.closest("a[href]") : null;
      if (link && /^da:/.test(link.getAttribute("href") || "")) {
        ev.preventDefault();
        ev.stopPropagation();
        window.parent.postMessage({ type: "da-nav", target: link.getAttribute("href") }, "*");
      }
      return;
    }
    ev.preventDefault();
    ev.stopPropagation();
    var el = ev.target;
    if (!el || el.nodeType !== 1) return;
    var exact = (el.textContent || "").trim().replace(/\s+/g, " ").slice(0, 200);
    window.parent.postMessage({
      type: "da-element-picked",
      selector: cssPath(el),
      exact: exact,
      tag: el.tagName.toLowerCase(),
    }, "*");
  }, true);
})();
