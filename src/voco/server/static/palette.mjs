// @ts-check
/**
 * ⌘K command palette (mk3, minimal by decision — navigation + mic).
 * One overlay, one input, arrow keys + enter, esc closes. Items are
 * built fresh from the store on every open: works, their open pages,
 * agents (view / patch mic), console tabs. Fuzzy = ordered-substring
 * scoring, nothing clever. The full action palette (annotate, export,
 * settings keys…) is deferred — see BUILD-CONSOLE.md M9.
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

/** Ordered-substring fuzzy score: lower is better, null = no match. */
function score(q, s) {
  s = s.toLowerCase();
  let i = 0, gaps = 0, last = -1;
  for (const ch of q) {
    i = s.indexOf(ch, last + 1);
    if (i < 0) return null;
    if (last >= 0 && i > last + 1) gaps++;
    last = i;
  }
  return gaps + (s.length - q.length) * 0.01;
}

let overlay = /** @type {?HTMLElement} */ (null);

export function closePalette() {
  if (overlay) { overlay.remove(); overlay = null; }
}

/**
 * @param {{items: {label:string, hint?:string, run:()=>void}[]}} ctx
 */
export function openPalette(ctx) {
  closePalette();
  const input = /** @type {HTMLInputElement} */ (el("input", {
    class: "pal-input", type: "text", placeholder: "jump to work · page · agent…" }));
  const list = el("div", { class: "pal-list" });
  const box = el("div", { class: "pal-box" }, input, list);
  const scrim = el("div", { class: "pal-scrim" }, box);
  overlay = scrim;
  scrim.addEventListener("mousedown", (e) => {
    if (e.target === scrim) closePalette();
  });
  document.body.append(scrim);

  let hits = ctx.items;
  let sel = 0;
  const renderList = () => {
    list.replaceChildren();
    hits.slice(0, 12).forEach((it, i) => {
      list.append(el("div", { class: "pal-item" + (i === sel ? " on" : ""),
        onclick: () => { closePalette(); it.run(); } },
        el("span", { class: "pal-label", text: it.label }),
        it.hint ? el("span", { class: "pal-hint", text: it.hint }) : null));
    });
    if (!hits.length)
      list.append(el("div", { class: "pal-item none", text: "no matches" }));
  };
  const refilter = () => {
    const q = input.value.trim().toLowerCase();
    hits = !q ? ctx.items
      : ctx.items
        .map((it) => ({ it, s: score(q, it.label) }))
        .filter((x) => x.s != null)
        .sort((a, b) => /** @type {number} */ (a.s) - /** @type {number} */ (b.s))
        .map((x) => x.it);
    sel = 0;
    renderList();
  };
  input.addEventListener("input", refilter);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); closePalette(); }
    else if (e.key === "ArrowDown") { e.preventDefault(); sel = Math.min(sel + 1, Math.min(hits.length, 12) - 1); renderList(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); sel = Math.max(sel - 1, 0); renderList(); }
    else if (e.key === "Enter") {
      e.preventDefault();
      const it = hits[sel];
      if (it) { closePalette(); it.run(); }
    }
  });
  refilter();
  input.focus();
}

export function paletteOpen() { return !!overlay; }
