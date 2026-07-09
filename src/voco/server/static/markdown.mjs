// @ts-check
/**
 * Shared markdown renderer (SPEC-WORKBENCH §7). Vendored marked + DOMPurify,
 * both pinned and served locally. If a vendor module fails to load, degrade
 * to escaped plaintext in a <pre> — never inject unsanitized HTML.
 *
 * Agent-supplied markdown is untrusted: it always passes through DOMPurify.
 *
 * M4: code fences highlight via vendored highlight.js (guarded — a missing
 * vendor never blocks rendering), shared with the files source view.
 */

let _marked = null;
let _purify = null;
let _hljs = null;
let _loaded = false;

async function ensure() {
  if (_loaded) return;
  _loaded = true;
  try {
    const [{ marked }, purify] = await Promise.all([
      import("./vendor/marked.mjs"),
      import("./vendor/purify.mjs"),
    ]);
    _marked = marked;
    _purify = purify.default || purify;
  } catch (e) {
    console.warn("markdown vendor unavailable; plaintext fallback", e);
  }
  try {
    _hljs = (await import("./vendor/highlight.mjs")).hljs || null;
  } catch (e) {
    console.warn("highlight vendor unavailable; plain code blocks", e);
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

/** Highlight one <code> element in place, guarded (reference parity). */
export async function highlightCode(codeEl, lang) {
  await ensure();
  if (!_hljs) return;
  try {
    if (lang && _hljs.getLanguage(lang)) {
      const r = _hljs.highlight(codeEl.textContent || "", { language: lang });
      codeEl.innerHTML = r.value; // hljs output is escaped by construction
      codeEl.classList.add("hljs");
    } else {
      _hljs.highlightElement(codeEl);
    }
  } catch (e) { /* plain text is a fine outcome */ }
}

/**
 * Render markdown into a container safely.
 * @param {HTMLElement} el @param {string} md
 */
export async function renderMarkdown(el, md) {
  await ensure();
  if (_marked && _purify) {
    const raw = _marked.parse(md || "", { breaks: false, gfm: true });
    el.innerHTML = _purify.sanitize(raw, { USE_PROFILES: { html: true } });
    for (const code of el.querySelectorAll("pre code")) {
      const m = /language-([\w-]+)/.exec(code.className || "");
      highlightCode(/** @type {HTMLElement} */ (code), m ? m[1] : "");
    }
  } else {
    el.innerHTML = `<pre>${escapeHtml(md || "")}</pre>`;
  }
  el.classList.add("md");
}
