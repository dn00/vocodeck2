// @ts-check
/**
 * Shared jump-back mechanics (B1a) — ported kernel from the
 * diff-annotate reference (lib/client/reveal.mjs): clicking a finding
 * lands on the anchored thing, flash-highlighted. ONE implementation
 * for every surface (diff rows, doc passages, later html elements), so
 * reveal behaves identically everywhere.
 *
 * `scroller` matters: scrollIntoView walks EVERY scrollable ancestor
 * (the deck's "stuck at the bottom" bug), so flash() takes the owning
 * scroll container and moves only it.
 */

export function cssEsc(s) {
  try { return CSS.escape(String(s)); }
  catch (e) { return String(s).replace(/["\\\]]/g, "\\$&"); }
}

/** Pulse an element, scrolling only `scroller` (or its nearest scroll
 * parent when omitted). False when el is missing.
 * @param {?Element} el @param {?HTMLElement} [scroller] */
export function flash(el, scroller = null) {
  if (!el) return false;
  const box = scroller || nearestScroller(el);
  if (box) {
    const br = box.getBoundingClientRect();
    const er = el.getBoundingClientRect();
    box.scrollTop += er.top - br.top - Math.max(24, br.height / 2 - er.height / 2);
  }
  el.classList.remove("reveal-flash");
  void (/** @type {HTMLElement} */ (el).offsetWidth); // restart animation
  el.classList.add("reveal-flash");
  setTimeout(() => el.classList.remove("reveal-flash"), 1600);
  return true;
}

function nearestScroller(el) {
  for (let n = el.parentElement; n; n = n.parentElement) {
    const o = getComputedStyle(n).overflowY;
    if ((o === "auto" || o === "scroll") && n.scrollHeight > n.clientHeight)
      return n;
  }
  return null;
}

/** DEEPEST element under `root` whose textContent contains `exact` —
 * element-level (not text-node) matching, so an anchor spanning inline
 * markup (`foo <code>bar</code>`) still reveals (xai B1a W4). Short
 * anchors down to 2 chars are allowed; ambiguity resolves to the
 * smallest matching element (xai W5).
 * @param {?Element} root @param {?string} exact */
export function findByText(root, exact) {
  const needle = String(exact == null ? "" : exact).trim().slice(0, 80);
  if (!root || needle.length < 2) return null;
  let best = (root.textContent || "").includes(needle) ? root : null;
  if (!best) return null;
  let advanced = true;
  while (advanced) {
    advanced = false;
    for (const child of best.children) {
      if ((child.textContent || "").includes(needle)) {
        best = child;
        advanced = true;
        break;
      }
    }
  }
  return best;
}
