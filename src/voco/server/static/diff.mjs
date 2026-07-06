// @ts-check
/**
 * Diff view + line annotation (SPEC-WORKBENCH §3, W1). Renders the parsed
 * diff as a unified hunk table whose rows carry data-file/data-side/data-line
 * so an annotation anchors to exactly the line clicked (never inferred).
 * Click a row to open a concern editor; shift-click to end a range.
 *
 * @typedef {{file:string, side:string, startLine:number, endLine:number}} Anchor
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
 * @param {{files:any[], interdiff?:?{since_rev:number, changed:string[],
 *   added:string[], removed:string[], unchanged:string[]}}} content
 * @param {{onAnnotate:(a:Anchor, text:string, kind:string)=>void,
 *   findings:any[], rev?:number}} ctx
 */
export function renderDiff(view, content, ctx) {
  view.replaceChildren();
  const wrap = el("div", { class: "diff" });
  let anchorStart = /** @type {?{file:string, side:string, line:number}} */ (null);

  // W5 since-rev banner: what moved since the rev this push replaced.
  const inter = content.interdiff;
  if (inter) {
    const bits = [];
    for (const k of ["changed", "added", "removed", "unchanged"])
      if (inter[k].length) bits.push(`${inter[k].length} ${k}`);
    const banner = el("div", { class: "rev-banner" },
      el("b", { text: `rev ${ctx.rev ?? "?"}` }),
      el("span", { text:
        ` — since rev ${inter.since_rev}: ${bits.join(", ") || "no changes"}` }));
    if (inter.removed.length)
      banner.append(el("span", { class: "rev-removed",
        text: ` (removed: ${inter.removed.join(", ")})` }));
    wrap.append(banner);
  }
  const fileChip = (path) => {
    if (!inter) return null;
    if (inter.changed.includes(path))
      return { cls: "inter-changed", label: `changed since r${inter.since_rev}` };
    if (inter.added.includes(path))
      return { cls: "inter-added", label: `added since r${inter.since_rev}` };
    if (inter.unchanged.includes(path))
      return { cls: "inter-unchanged", label: "unchanged" };
    return null;
  };

  // Index findings by file+side+line for margin markers.
  const byLine = new Map();
  for (const f of ctx.findings || []) {
    const a = f.anchor || {};
    for (let ln = a.startLine; ln <= (a.endLine || a.startLine); ln++)
      byLine.set(`${a.file}:${a.side}:${ln}`, f);
  }

  for (const file of content.files || []) {
    const chip = fileChip(file.path);
    wrap.append(el("div", { class: "diff-file" },
      el("span", { class: "diff-path", text: file.path }),
      file.is_new ? el("span", { class: "diff-tag add", text: "new" }) : null,
      file.is_deleted ? el("span", { class: "diff-tag del", text: "del" }) : null,
      chip ? el("span", { class: "diff-tag " + chip.cls, text: chip.label }) : null));
    const table = el("div", { class: "diff-hunks" });
    for (const hunk of file.hunks || []) {
      table.append(el("div", { class: "hunk-head", text: hunk.header }));
      for (const row of hunk.rows) {
        const key = `${file.path}:${row.side}:${row.line}`;
        const tr = el("div", {
          class: "drow " + row.kind + (byLine.has(key) ? " flagged" : ""),
          "data-file": file.path, "data-side": row.side,
          "data-line": row.line == null ? "" : row.line,
        },
          el("span", { class: "gutter old", text: row.old_line ?? "" }),
          el("span", { class: "gutter new", text: row.new_line ?? "" }),
          el("span", { class: "sign", text: row.kind === "add" ? "+" : row.kind === "del" ? "-" : " " }),
          el("span", { class: "code", text: row.content }));
        if (row.line != null) {
          tr.addEventListener("click", (e) => {
            if (e.shiftKey && anchorStart && anchorStart.file === file.path
                && anchorStart.side === row.side) {
              const lo = Math.min(anchorStart.line, row.line);
              const hi = Math.max(anchorStart.line, row.line);
              openEditor(tr, { file: file.path, side: row.side, startLine: lo, endLine: hi });
            } else {
              anchorStart = { file: file.path, side: row.side, line: row.line };
              openEditor(tr, { file: file.path, side: row.side,
                startLine: row.line, endLine: row.line });
            }
          });
        }
        table.append(tr);
      }
    }
    wrap.append(table);
  }
  view.append(wrap);

  /** @param {HTMLElement} afterRow @param {Anchor} anchor */
  function openEditor(afterRow, anchor) {
    view.querySelectorAll(".annot-editor").forEach((n) => n.remove());
    const ta = el("textarea", { class: "annot-input", rows: "2",
      placeholder: anchor.startLine === anchor.endLine
        ? `concern on ${anchor.file}:${anchor.startLine}`
        : `concern on ${anchor.file}:${anchor.startLine}–${anchor.endLine}` });
    const kind = el("select", { class: "annot-kind" });
    for (const k of ["concern", "question", "nit"])
      kind.append(el("option", { value: k, text: k }));
    const save = el("button", { class: "btn", text: "flag",
      onclick: () => {
        const text = /** @type {HTMLTextAreaElement} */ (ta).value.trim();
        if (!text) return;
        ctx.onAnnotate(anchor, text, /** @type {HTMLSelectElement} */ (kind).value);
        box.remove();
      } });
    const cancel = el("button", { class: "btn ghost", text: "✕",
      onclick: () => box.remove() });
    const box = el("div", { class: "annot-editor" },
      el("div", { class: "annot-row" }, kind, save, cancel), ta);
    afterRow.after(box);
    /** @type {HTMLTextAreaElement} */ (ta).focus();
  }
}
