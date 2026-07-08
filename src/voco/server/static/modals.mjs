// @ts-check
/**
 * Modals (DESIGN-DECK rev 4.1: the review picker, spawn, connect, open-
 * repo). The ONLY place CLI one-liners appear is the connect modal.
 * Esc closes (keyboard floor); Enter submits where a single action is
 * obvious. Spawn is tmux-only — the pty entrypoint is FROZEN by
 * decision (grill 2026-07-07): the code stays, the UI doesn't offer it.
 *
 * @typedef {{command:(cmd:string, payload?:object)=>Promise<any>,
 *   toast:(msg:string, sticky?:boolean)=>void}} Ctx
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

const errMsg = (e) => (e instanceof Error ? e.message : String(e));

let activeClose = /** @type {?() => void} */ (null);

/** One modal at a time; returns close(). onClose fires however the
 * modal dies (action, Esc, scrim click, replacement) — exactly once.
 * @param {string} title @param {?string} where @param {any[]} body
 * @param {any[]} actions @param {?(() => void)} [onClose] */
function openModal(title, where, body, actions, onClose = null) {
  if (activeClose) activeClose(); // close() removes ITS key listener too
  const modal = el("div", { class: "modal", role: "dialog", "aria-label": title },
    el("h4", { text: title }),
    where ? el("div", { class: "pwhere", text: where }) : null,
    ...body,
    el("div", { class: "pactions" }, ...actions));
  const scrim = el("div", { class: "scrim on" }, modal);
  scrim.addEventListener("click", (e) => { if (e.target === scrim) close(); });
  const onKey = (e) => { if (e.key === "Escape") close(); };
  document.addEventListener("keydown", onKey);
  let closed = false;
  function close() {
    if (closed) return;
    closed = true;
    scrim.remove();
    document.removeEventListener("keydown", onKey);
    if (activeClose === close) activeClose = null;
    if (onClose) onClose();
  }
  document.body.append(scrim);
  activeClose = close;
  return close;
}

/** In-deck confirm for kill-class actions ONLY (destructive policy:
 * everything reversible gets undo instead). Native confirm() is banned
 * — it freezes the event loop and every voice surface with it. */
export function confirmDanger(title, detail, action) {
  return new Promise((resolve) => {
    let decided = false;
    const done = (v, close) => { decided = true; resolve(v); if (close) close(); };
    const close = openModal(title, detail, [], [
      el("button", { class: "btn-ghost", text: "cancel",
        onclick: () => done(false, close) }),
      el("button", { class: "btn-primary danger", text: action,
        onclick: () => done(true, close) }),
    ], () => { if (!decided) resolve(false); });
  });
}

function seg(options, initial, onPick) {
  const box = el("div", { class: "seg" });
  let current = initial;
  const btns = options.map((o) => {
    const b = el("button", { class: o === initial ? "on" : "", text: o,
      onclick: () => {
        current = o;
        for (const x of btns) x.classList.toggle("on", x.textContent === o);
        onPick(o);
      } });
    return b;
  });
  box.append(...btns);
  return { box, get value() { return current; } };
}

const input = (attrs = {}) =>
  /** @type {HTMLInputElement} */ (el("input", { type: "text", ...attrs }));

// ---- review picker (U2c): pick a source, the daemon resolves it ---------------
/** @param {Ctx & {ws:any, onOpened:(r:any, wsKey:string)=>void}} ctx */
export function openPicker(ctx) {
  const ws = ctx.ws;
  const base = input({ value: "", placeholder: "origin/main" });
  const prNum = input({ placeholder: "number" });
  const fields = el("div", {});
  const hint = el("div", { class: "hintline" });
  const mode = seg(["branch", "pr #", "staged"], "branch", render);
  function render() {
    fields.replaceChildren();
    if (mode.value === "branch") {
      fields.append(el("label", { text: "against base" }), base);
      hint.textContent =
        "empty = the repo's default branch · resolved by the daemon in the workspace root";
    } else if (mode.value === "pr #") {
      fields.append(el("label", { text: "PR number" }), prNum);
      hint.textContent = "needs gh + auth in the workspace root";
    } else {
      hint.textContent = "the workspace's staged changes, as git sees them now";
    }
  }
  render();
  async function open() {
    let source;
    if (mode.value === "branch") source = { branch: base.value.trim() };
    else if (mode.value === "pr #") {
      const n = parseInt(prNum.value.trim(), 10);
      if (!Number.isFinite(n)) { ctx.toast("PR needs a number", true); return; }
      source = { pr: n };
    } else source = { staged: true };
    try {
      const r = await ctx.command("page.publish", { workspace: ws.key, source });
      close();
      ctx.onOpened(r, ws.key);
    } catch (e) { ctx.toast("review failed: " + errMsg(e), true); }
  }
  for (const f of [base, prNum])
    f.addEventListener("keydown", (e) => { if (e.key === "Enter") open(); });
  const close = openModal("Review a diff",
    `${ws.repo || ws.name} · ${ws.root}${ws.branch ? " · " + ws.branch : ""}`,
    [mode.box, fields, hint],
    [el("button", { class: "btn-ghost", text: "cancel", onclick: () => close() }),
      el("button", { class: "btn-primary", text: "open diff", onclick: open })]);
}

// ---- open a repo (U2c empty state): mint a workspace from a path --------------
/** @param {Ctx & {onOpened:(wsKey:string)=>void}} ctx */
export function openRepo(ctx) {
  const path = input({ placeholder: "~/code/my-repo" });
  async function open() {
    const p = path.value.trim();
    if (!p) return;
    try {
      const r = await ctx.command("workspace.open", { path: p });
      close();
      ctx.toast(`opened ${r.repo || r.root}`);
      ctx.onOpened(r.workspace);
    } catch (e) { ctx.toast("open failed: " + errMsg(e), true); }
  }
  path.addEventListener("keydown", (e) => { if (e.key === "Enter") open(); });
  const close = openModal("Open a repo",
    "a git checkout on this machine — its work appears in the rail",
    [el("label", { text: "path" }), path],
    [el("button", { class: "btn-ghost", text: "cancel", onclick: () => close() }),
      el("button", { class: "btn-primary", text: "open", onclick: open })]);
  path.focus();
}

// ---- spawn (U2d): managed tmux session, optional fresh worktree ----------------
/** @param {Ctx & {rootHint?:string}} ctx */
export function openSpawn(ctx) {
  const custom = input({ placeholder: "command, e.g. aider" });
  const repo = input({ value: ctx.rootHint || "", placeholder: "~/code/my-repo" });
  const wt = input({ placeholder: "branch name — spawns in a fresh sibling worktree" });
  const customRow = el("div", {});
  const harness = seg(["claude", "codex", "custom…"], "claude", (v) => {
    customRow.replaceChildren();
    if (v === "custom…") customRow.append(el("label", { text: "command" }), custom);
  });
  async function spawn() {
    const cmd = harness.value === "custom…" ? custom.value.trim() : harness.value;
    if (!cmd) { ctx.toast("harness command required", true); return; }
    const payload = { harness: cmd, cwd: repo.value.trim() || undefined };
    const branch = wt.value.trim();
    if (branch) payload.worktree = { branch };
    try {
      const r = await ctx.command("session.spawn", payload);
      close();
      ctx.toast(`spawned in tmux: ${r.tmux_session}`
        + (r.worktree ? ` · worktree ${r.worktree}` : ""));
    } catch (e) { ctx.toast("spawn failed: " + errMsg(e), true); }
  }
  const close = openModal("Spawn an agent",
    "runs inside tmux — survives daemon restarts; attach natively any time",
    [el("label", { text: "harness" }), harness.box, customRow,
      el("label", { text: "repo" }), repo,
      el("label", { text: "worktree (optional)" }), wt,
      el("div", { class: "hintline",
        text: "a worktree spawn names the session after its branch; clean worktrees are reaped on kill, dirty ones never" })],
    [el("button", { class: "btn-ghost", text: "cancel", onclick: () => close() }),
      el("button", { class: "btn-primary", text: "spawn", onclick: spawn })]);
}

// ---- settings (mockup rev 4.1, pulled forward from U3) --------------------------
// config.get/set are the whole backend. Honest hot-apply: keys in the
// daemon's _hot list apply live; every other row is marked "restart" IN
// ADVANCE, and the toast reports what actually happened.
/** @param {Ctx} ctx */
export async function openSettings(ctx) {
  let cfg;
  try { cfg = await ctx.command("config.get", {}); }
  catch (e) { ctx.toast("settings unavailable: " + errMsg(e), true); return; }
  const hot = new Set(cfg._hot || []);
  const body = [];
  const parse = (raw, old) => {
    if (typeof old === "boolean") return raw === "true";
    if (typeof old === "number") {
      const n = Number(raw);
      if (!Number.isFinite(n)) throw new Error("needs a number");
      return n;
    }
    return raw;
  };
  for (const section of Object.keys(cfg).sort()) {
    const values = cfg[section];
    if (section.startsWith("_") || typeof values !== "object" || values === null)
      continue;
    body.push(el("div", { class: "set-h", text: section }));
    for (const key of Object.keys(values).sort()) {
      const val = values[key];
      if (typeof val === "object" && val !== null) continue; // scalars only
      const full = `${section}.${key}`;
      const live = hot.has(full);
      let field;
      if (typeof val === "boolean") {
        field = el("select", {});
        for (const o of ["true", "false"])
          field.append(el("option", { value: o, text: o,
            selected: String(val) === o ? "" : null }));
      } else {
        field = el("input", { type: "text", value: String(val) });
      }
      const apply = async () => {
        let parsed;
        try { parsed = parse(/** @type {any} */ (field).value, val); }
        catch (e) { ctx.toast(`${full}: ${errMsg(e)}`, true); return; }
        if (parsed === val) return;
        try {
          const r = await ctx.command("config.set", { key: full, value: parsed });
          ctx.toast(r.applied ? `${full} applied live`
            : `${full} saved — takes effect on restart`);
        } catch (e) { ctx.toast(`${full}: ${errMsg(e)}`, true); }
      };
      field.addEventListener("change", apply);
      field.addEventListener("keydown", (e) => {
        if (e.key === "Enter") apply();
      });
      body.push(el("div", { class: "set-row" },
        el("span", { class: "sk", text: key },
          el("small", { text: live ? "hot-applies" : "restart" })),
        field));
    }
  }
  if (!body.length)
    body.push(el("div", { class: "hintline",
      text: "no editable config — the daemon is running on defaults" }));
  const close = openModal("Settings",
    "written to the overrides file · unmarked keys need a daemon restart",
    body,
    [el("button", { class: "btn-primary", text: "done", onclick: () => close() })]);
}

// ---- connect (U2d): the paste-ready attach story --------------------------------
/** @param {Ctx} ctx */
export function openConnect(ctx) {
  const row = (label, small, code) => el("div", { class: "conn-row" },
    el("span", { class: "ck", text: label }, el("small", { text: small })),
    el("code", { text: code }),
    el("button", { text: "copy", onclick: async () => {
      try { await navigator.clipboard.writeText(code); ctx.toast("copied"); }
      catch (e) { ctx.toast("copy failed: " + errMsg(e), true); }
    } }));
  const rows = [
    row("Claude Code / Codex (MCP)", "call the voice_init tool once in the session",
      "voice_init"),
    row("any shell agent", "run in the repo the agent works in", "voco listen"),
    row("MCP server not configured yet?", "adds voco to Claude Code",
      "claude mcp add voco -- voco-mcp"),
  ];
  const remote = el("div", { class: "hintline", text: "" });
  const close = openModal("Connect an existing session",
    "for agents you already run yourself — one line each, then they appear in the rail",
    [...rows, remote],
    [el("button", { class: "btn-primary", text: "done", onclick: () => close() })]);
  // Enrich with the daemon's own attach facts (remote/ssh line) — quietly.
  ctx.command("attach.snippet", {}).then((s) => {
    if (s && s.remote) remote.textContent = "remote: " + s.remote;
  }).catch(() => {});
}
