// Vendored @xterm/xterm 5.5.0 (MIT) as an ES module. Buildless: the UMD
// bundle assigns its exports onto globalThis; we re-export Terminal.
// See vendor/MANIFEST.
import "./xterm.min.js";
export const Terminal = globalThis.Terminal;
