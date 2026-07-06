// @ts-check
/**
 * Shared markdown renderer (SPEC-WORKBENCH §7). Vendored marked + DOMPurify,
 * both pinned and served locally. If a vendor module fails to load, degrade
 * to escaped plaintext in a <pre> — never inject unsanitized HTML.
 *
 * Agent-supplied markdown is untrusted: it always passes through DOMPurify.
 */

let _marked = null;
let _purify = null;
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
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
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
  } else {
    el.innerHTML = `<pre>${escapeHtml(md || "")}</pre>`;
  }
  el.classList.add("md");
}
