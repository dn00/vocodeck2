// @ts-check
/**
 * The icon sheet (index7 FL push, lever 1) — one inline-SVG symbol set,
 * 14px, currentColor, replacing the unicode glyph grab-bag. Symbols are
 * verbatim from design/index7.html; add there first, then here.
 *
 * SVG nodes cannot be made with document.createElement (they need the
 * SVG namespace), so ic() is the one shared builder — the el()/h()
 * helpers in other modules stay HTML-only.
 */

const SVG_NS = "http://www.w3.org/2000/svg";
const SHEET_ID = "voco-icon-sheet";

const SHEET = `
  <symbol id="i-module" viewBox="0 0 14 14"><rect x="2.5" y="1.5" width="9" height="11" fill="none" stroke="currentColor"/><rect x="5" y="4" width="4" height="3" fill="currentColor"/><path d="M5 9.5h4M5 11h2.5" stroke="currentColor"/><path d="M0.5 4h2M0.5 7h2M0.5 10h2M11.5 4h2M11.5 7h2M11.5 10h2" stroke="currentColor"/></symbol>
  <symbol id="i-overview" viewBox="0 0 14 14"><rect x="1.5" y="1.5" width="4.5" height="4.5" fill="currentColor"/><rect x="8" y="1.5" width="4.5" height="4.5" fill="none" stroke="currentColor"/><rect x="1.5" y="8" width="4.5" height="4.5" fill="none" stroke="currentColor"/><rect x="8" y="8" width="4.5" height="4.5" fill="none" stroke="currentColor"/></symbol>
  <symbol id="i-screen" viewBox="0 0 14 14"><rect x="1.5" y="2.5" width="11" height="8" fill="none" stroke="currentColor"/><path d="M4 12.5h6" stroke="currentColor"/><path d="M3.5 5h7M3.5 7h4" stroke="currentColor"/></symbol>
  <symbol id="i-diff" viewBox="0 0 14 14"><path d="M4 3.5h6M7 1v5" stroke="currentColor"/><path d="M4 10.5h6" stroke="currentColor"/></symbol>
  <symbol id="i-files" viewBox="0 0 14 14"><path d="M2 2.5h4l1.5 2H12v7H2z" fill="none" stroke="currentColor"/></symbol>
  <symbol id="i-doc" viewBox="0 0 14 14"><path d="M3 2.5h8M3 5h8M3 7.5h8M3 10h5" stroke="currentColor"/></symbol>
  <symbol id="i-term" viewBox="0 0 14 14"><path d="M2.5 3l4 4-4 4" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M8 11h4" stroke="currentColor" stroke-width="1.5"/></symbol>
  <symbol id="i-branch" viewBox="0 0 14 14"><circle cx="3.5" cy="3" r="1.6" fill="none" stroke="currentColor"/><circle cx="3.5" cy="11" r="1.6" fill="none" stroke="currentColor"/><circle cx="10.5" cy="5" r="1.6" fill="none" stroke="currentColor"/><path d="M3.5 4.6v4.8M10.5 6.6c0 2.5-4 2-5.5 3" fill="none" stroke="currentColor"/></symbol>
  <symbol id="i-rec" viewBox="0 0 10 10"><circle cx="5" cy="5" r="3.6" fill="currentColor"/></symbol>
  <symbol id="i-stop" viewBox="0 0 10 10"><rect x="1.5" y="1.5" width="7" height="7" fill="currentColor"/></symbol>
  <symbol id="i-lock" viewBox="0 0 14 14"><rect x="3" y="6" width="8" height="6" fill="none" stroke="currentColor"/><path d="M4.7 6V4.3a2.3 2.3 0 014.6 0V6" fill="none" stroke="currentColor"/></symbol>
  <symbol id="i-mic" viewBox="0 0 14 14"><rect x="5.2" y="1.5" width="3.6" height="7" fill="none" stroke="currentColor"/><path d="M3 7.5a4 4 0 008 0M7 11.5v1.5" fill="none" stroke="currentColor"/></symbol>
`;

/** Inject the hidden symbol sheet once; safe to call repeatedly. */
export function installIcons() {
  if (document.getElementById(SHEET_ID)) return;
  const sheet = document.createElementNS(SVG_NS, "svg");
  sheet.setAttribute("id", SHEET_ID);
  sheet.setAttribute("width", "0");
  sheet.setAttribute("height", "0");
  sheet.setAttribute("style", "position:absolute");
  sheet.innerHTML = SHEET;
  document.body.append(sheet);
}

/**
 * An icon instance: <svg class="ic"><use href="#i-name"/></svg>.
 * @param {string} name  symbol name without the "i-" prefix
 * @param {string} [cls] class list (default "ic"; add s10/s9 to shrink)
 */
export function ic(name, cls = "ic") {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("class", cls);
  const use = document.createElementNS(SVG_NS, "use");
  use.setAttribute("href", "#i-" + name);
  svg.append(use);
  return svg;
}
