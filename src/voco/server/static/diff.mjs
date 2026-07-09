// @ts-check
/**
 * Diff view (SPEC-WORKBENCH §3, W1; DESIGN-DECK rev 4.1 look, U2c) —
 * a collapsed-by-default FILE INDEX. Each file is a fold (▸/▾ head with
 * ± stats, open-annotation tag, since-rev chip); the caller seeds which
 * folds start open (open annotations / changed-since files) and owns the
 * fold Set so re-renders keep the reader's place. Rows carry
 * data-file/data-side/data-line so an annotation anchors to exactly the
 * line clicked (never inferred). Click a row for the inline editor
 * (diff-annotate reference structure verbatim: target line, textarea,
 * concern|question|nit pills, blocking, tip); shift-click ends a range;
 * ctrl/cmd+enter adds.
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

/** Totals + per-file ± counts — the work-head and the rail sub-tree
 * both read from this. @param {any[]} files */
export function diffStats(files) {
  const perFile = new Map();
  let add = 0, del = 0;
  for (const f of files || []) {
    let a = 0, d = 0;
    for (const h of f.hunks || [])
      for (const r of h.rows || []) {
        if (r.kind === "add") a++;
        else if (r.kind === "del") d++;
      }
    perFile.set(f.path, { add: a, del: d });
    add += a; del += d;
  }
  return { files: (files || []).length, add, del, perFile };
}

/** Which files deserve to start open: open annotations or moved since
 * the last rev. @param {any} content @param {any[]} findings */
export function seedFolds(content, findings) {
  const open = new Set();
  const flagged = new Set(
    (findings || []).filter((f) => f.status === "open")
      .map((f) => (f.anchor || {}).file));
  const inter = content.interdiff;
  for (const f of content.files || []) {
    if (flagged.has(f.path)) open.add(f.path);
    else if (inter && (inter.changed.includes(f.path) || inter.added.includes(f.path)))
      open.add(f.path);
  }
  return open;
}

/**
 * @param {HTMLElement} view
 * @param {{files:any[], interdiff?:?{since_rev:number, changed:string[],
 *   added:string[], removed:string[], unchanged:string[]}}} content
 * @param {{onAnnotate:(a:Anchor, text:string, kind:string, blocking:boolean)=>void,
 *   findings:any[], rev?:number, fold:Set<string>, reveal?:?string,
 *   scrollTo?:(el:HTMLElement)=>void, onFoldChange?:()=>void,
 *   highlight?:(codeEl:HTMLElement, path:string)=>any,
 *   reviewed?:Set<string>, onReviewToggle?:(path:string, on:boolean)=>void,
 *   onAsk?:(a:Anchor, text:string)=>void}} ctx
 * @returns {{expandAll:(open:boolean)=>void, allOpen:boolean,
 *   nextChange:()=>void, prevChange:()=>void}}
 */
export function renderDiff(view, content, ctx) {
  view.replaceChildren();
  const wrap = el("div", { class: "diff" });
  let anchorStart = /** @type {?{file:string, side:string, line:number}} */ (null);
  const inter = content.interdiff;
  const fold = ctx.fold;

  // #10: syntax highlighting is LAZY per file (first open) and capped —
  // a 20k-row diff must not pay hljs for folded files it never shows.
  const HIGHLIGHT_ROW_CAP = 800;
  /** @type {Map<string, {list:HTMLElement[], done:boolean}>} */
  const codeByFile = new Map();
  const highlightFile = (path) => {
    if (!ctx.highlight) return;
    const entry = codeByFile.get(path);
    if (!entry || entry.done) return;
    entry.done = true;
    if (entry.list.length > HIGHLIGHT_ROW_CAP) return; // perf guard
    for (const c of entry.list) ctx.highlight(c, path);
  };

  // #15: j/k targets — the first row of every changed run.
  /** @type {{el:HTMLElement, path:string}[]} */
  const changeBlocks = [];

  const fileChip = (path) => {
    if (!inter) return null;
    if (inter.changed.includes(path))
      return { cls: "inter-changed", label: `changed since r${inter.since_rev}` };
    if (inter.added.includes(path))
      return { cls: "inter-added", label: `added since r${inter.since_rev}` };
    return null;
  };

  // Findings indexed by file (head tags) and by exact line (row marks).
  const byLine = new Map();
  const openByFile = new Map();
  for (const f of ctx.findings || []) {
    if (f.status !== "open") continue; // marks mean "needs you" — only open
    const a = f.anchor || {};
    for (let ln = a.startLine; ln <= (a.endLine || a.startLine); ln++)
      byLine.set(`${a.file}:${a.side}:${ln}`, f);
    openByFile.set(a.file, (openByFile.get(a.file) || 0) + 1);
  }

  const stats = diffStats(content.files);
  const sections = new Map();
  for (const file of content.files || []) {
    const st = stats.perFile.get(file.path) || { add: 0, del: 0 };
    const chip = fileChip(file.path);
    const openN = openByFile.get(file.path) || 0;
    const isRev = !!(ctx.reviewed && ctx.reviewed.has(file.path));
    const section = el("div", {
      class: "dfile" + (fold.has(file.path) ? " open" : "")
        + (isRev ? " reviewed" : ""),
      "data-path": file.path,
    });
    const tri = el("span", { class: "tri", text: fold.has(file.path) ? "▾" : "▸" });
    const rvw = el("span", { class: "rvw" + (isRev ? " on" : ""),
      title: "mark file reviewed (per revision)",
      text: isRev ? "✓ reviewed" : "✓" });
    rvw.addEventListener("click", (e) => {
      e.stopPropagation();
      const now = !section.classList.contains("reviewed");
      section.classList.toggle("reviewed", now);
      rvw.classList.toggle("on", now);
      rvw.textContent = now ? "✓ reviewed" : "✓";
      if (now && fold.has(file.path)) { // a reviewed file folds away
        fold.delete(file.path);
        section.classList.remove("open");
        tri.textContent = "▸";
        if (ctx.onFoldChange) ctx.onFoldChange();
      }
      if (ctx.onReviewToggle) ctx.onReviewToggle(file.path, now);
    });
    const head = el("div", { class: "dfile-head" },
      tri,
      el("span", { text: file.path }),
      file.is_new ? el("span", { class: "diff-tag add", text: "new" }) : null,
      file.is_deleted ? el("span", { class: "diff-tag del", text: "del" }) : null,
      el("span", { class: "dstat" },
        el("span", { class: "add", text: `+${st.add}` }),
        " ",
        el("span", { class: "del", text: `−${st.del}` })),
      el("span", { class: "fchips" },
        openN ? el("span", { class: "tag open", text: `${openN} open` }) : null,
        chip ? el("span", { class: "micro " + chip.cls, text: chip.label }) : null,
        rvw));
    head.addEventListener("click", () => {
      const now = !fold.has(file.path);
      if (now) fold.add(file.path); else fold.delete(file.path);
      section.classList.toggle("open", now);
      tri.textContent = now ? "▾" : "▸";
      if (now) highlightFile(file.path);
      if (ctx.onFoldChange) ctx.onFoldChange();
    });
    const body = el("div", { class: "dfile-body" });
    const codes = { list: /** @type {HTMLElement[]} */ ([]), done: false };
    codeByFile.set(file.path, codes);
    let inChange = false;
    for (const hunk of file.hunks || []) {
      body.append(el("div", { class: "hunk-head", text: hunk.header }));
      inChange = false;
      for (const row of hunk.rows) {
        const key = `${file.path}:${row.side}:${row.line}`;
        const mark = byLine.get(key);
        const codeText = el("span", { class: "ct", text: row.content });
        codes.list.push(codeText);
        const tr = el("div", {
          class: "drow " + row.kind + (mark ? " flagged" : ""),
          "data-file": file.path, "data-side": row.side,
          "data-line": row.line == null ? "" : row.line,
        },
          el("span", { class: "gutter old", text: row.old_line ?? "" }),
          el("span", { class: "gutter new", text: row.new_line ?? "" }),
          el("span", { class: "sign", text: row.kind === "add" ? "+" : row.kind === "del" ? "-" : " " }),
          el("span", { class: "code" }, codeText,
            mark ? el("span", { class: "fmark", text: mark.finding_id }) : null));
        const changed = row.kind === "add" || row.kind === "del";
        if (changed && !inChange) changeBlocks.push({ el: tr, path: file.path });
        inChange = changed;
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
        body.append(tr);
      }
    }
    section.append(head, body);
    sections.set(file.path, section);
    wrap.append(section);
    if (fold.has(file.path)) highlightFile(file.path); // open at build
  }
  view.append(wrap);

  if (ctx.reveal && sections.has(ctx.reveal)) {
    const s = /** @type {HTMLElement} */ (sections.get(ctx.reveal));
    if (!fold.has(ctx.reveal)) {
      fold.add(ctx.reveal);
      s.classList.add("open");
      const t = s.querySelector(".tri");
      if (t) t.textContent = "▾";
      highlightFile(ctx.reveal);
    }
    // container-scoped: scrollIntoView would drag every ancestor along
    requestAnimationFrame(() =>
      ctx.scrollTo ? ctx.scrollTo(s) : s.scrollIntoView({ block: "start" }));
  }

  /** @param {HTMLElement} afterRow @param {Anchor} anchor */
  function openEditor(afterRow, anchor) {
    view.querySelectorAll(".annot-editor").forEach((n) => n.remove());
    view.querySelectorAll(".drow.selected").forEach((n) => n.classList.remove("selected"));
    markRange(anchor, true);
    let kind = "concern";
    const short = anchor.file.split("/").pop();
    const lines = anchor.startLine === anchor.endLine
      ? `${anchor.startLine}` : `${anchor.startLine}–${anchor.endLine}`;
    const target = el("div", { class: "editor-target" });
    const setTarget = () => {
      target.textContent = `${kind} for ${short}:${lines} (${anchor.side} side)`;
    };
    setTarget();
    const ta = /** @type {HTMLTextAreaElement} */ (el("textarea"));
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
    const close = () => { box.remove(); markRange(anchor, false); };
    const add = () => {
      const text = ta.value.trim();
      if (!text) return;
      ctx.onAnnotate(anchor, text, kind, blocking.checked);
      close();
    };
    ta.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); add(); }
    });
    const box = el("div", { class: "inline-editor annot-editor" },
      target, ta,
      pills,
      el("div", { class: "editor-actions" },
        el("button", { class: "tbtn primary", text: "add annotation", onclick: add }),
        ctx.onAsk ? el("button", { class: "tbtn", text: "ask",
          title: "ask the work's agents about these lines",
          onclick: () => {
            const text = ta.value.trim();
            if (!text) { ta.focus(); return; }
            /** @type {NonNullable<typeof ctx.onAsk>} */ (ctx.onAsk)(anchor, text);
            close();
          } }) : null,
        el("button", { class: "tbtn", text: "cancel", onclick: close })),
      el("div", { class: "flow-note",
        text: "tip: shift-click another line first to annotate a range. ctrl/cmd+enter to add." }));
    afterRow.after(box);
    ta.focus();
  }

  function markRange(anchor, on) {
    for (let ln = anchor.startLine; ln <= anchor.endLine; ln++) {
      const row = view.querySelector(
        `.drow[data-file="${cssq(anchor.file)}"][data-side="${anchor.side}"][data-line="${ln}"]`);
      if (row) row.classList.toggle("selected", on);
    }
  }
  const cssq = (s) => String(s).replace(/["\\]/g, "\\$&");

  // #15: walk change blocks; jumping into a folded file opens it.
  let navIdx = -1;
  function jumpTo(i) {
    if (!changeBlocks.length) return;
    navIdx = ((i % changeBlocks.length) + changeBlocks.length) % changeBlocks.length;
    const b = changeBlocks[navIdx];
    if (!fold.has(b.path)) {
      fold.add(b.path);
      const s = sections.get(b.path);
      if (s) {
        s.classList.add("open");
        const t = s.querySelector(".tri");
        if (t) t.textContent = "▾";
      }
      highlightFile(b.path);
      if (ctx.onFoldChange) ctx.onFoldChange();
    }
    if (ctx.scrollTo) ctx.scrollTo(b.el); else b.el.scrollIntoView();
    b.el.classList.remove("blink");
    void b.el.offsetWidth; // restart the animation
    b.el.classList.add("blink");
  }

  return {
    expandAll(open) {
      for (const [path, section] of sections) {
        if (open) fold.add(path); else fold.delete(path);
        section.classList.toggle("open", open);
        const t = section.querySelector(".tri");
        if (t) t.textContent = open ? "▾" : "▸";
        if (open) highlightFile(path);
      }
      if (ctx.onFoldChange) ctx.onFoldChange();
    },
    get allOpen() {
      return (content.files || []).every((f) => fold.has(f.path));
    },
    nextChange() { jumpTo(navIdx + 1); },
    prevChange() { jumpTo(navIdx - 1); },
  };
}
