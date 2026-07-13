import { expect, test } from "@playwright/test";
import { execFileSync, spawn } from "node:child_process";
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import os from "node:os";
import net from "node:net";
import path from "node:path";

const project = process.cwd();
let daemon;
let daemonOutput = "";
let root;
let repo;
let baseUrl;
let sessionId;
let identity;
let workspace;

function git(...args) {
  return execFileSync("git", args, { cwd: repo, encoding: "utf8" }).trim();
}

async function freePort() {
  const server = net.createServer();
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  const port = typeof address === "object" && address ? address.port : 0;
  await new Promise((resolve) => server.close(resolve));
  return port;
}

async function json(pathname, body) {
  const response = await fetch(baseUrl + pathname, {
    method: body === undefined ? "GET" : "POST",
    headers: body === undefined ? undefined : { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    throw new Error(`${pathname}: ${response.status} ${await response.text()}`);
  }
  return response.json();
}

async function waitForDaemon() {
  const deadline = Date.now() + 15_000;
  while (Date.now() < deadline) {
    if (daemon.exitCode !== null) {
      throw new Error(`daemon exited early (${daemon.exitCode})\n${daemonOutput}`);
    }
    try {
      const health = await json("/v1/health");
      if (health.ok && health.service === "voco-d") return;
    } catch {
      // Startup is asynchronous; retry until the bounded deadline.
    }
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error(`daemon did not become healthy\n${daemonOutput}`);
}

async function pushPage(body) {
  return json("/v1/bridge/page", { session_id: sessionId, identity, ...body });
}

test.beforeAll(async () => {
  test.setTimeout(30_000);
  root = mkdtempSync(path.join(tmpdir(), "voco-browser-e2e-"));
  repo = path.join(root, "repo");
  mkdirSync(repo);
  execFileSync("git", ["init", "-b", "main"], { cwd: repo });
  git("config", "user.email", "e2e@example.invalid");
  git("config", "user.name", "Voco E2E");
  writeFileSync(path.join(repo, "notes.md"), "# Notes\n\nold path content\n");
  writeFileSync(path.join(repo, "demo.py"), "value = 'old'\n");
  git("add", ".");
  git("commit", "-m", "seed");

  const port = await freePort();
  baseUrl = `http://127.0.0.1:${port}`;
  const config = path.join(root, "config.toml");
  writeFileSync(
    config,
    `[state]\ndir = ${JSON.stringify(path.join(root, "state"))}\n` +
      `[workbench]\ndata_dir = ${JSON.stringify(path.join(root, "workbench"))}\n` +
      `live_git_s = 0\n`,
  );
  daemon = spawn(
    "uv",
    ["run", "--project", project, "voco-d", "--config", config, "--no-audio", "--port", String(port)],
    { cwd: repo, stdio: ["ignore", "pipe", "pipe"] },
  );
  daemon.stdout.on("data", (chunk) => { daemonOutput += chunk; });
  daemon.stderr.on("data", (chunk) => { daemonOutput += chunk; });
  await waitForDaemon();

  identity = {
    host: os.hostname().split(".")[0],
    user: os.userInfo().username,
    cwd: repo,
    repo: "repo",
    branch: "main",
    worktree: repo,
    common_dir: git("rev-parse", "--path-format=absolute", "--git-common-dir"),
    harness: "e2e",
    pid: process.pid,
    instance: "browser-e2e",
    capabilities: ["say", "listen", "screen", "page", "review"],
  };
  const registered = await json("/v1/bridge/register", identity);
  sessionId = registered.session_id;
  await json("/v1/bridge/screen", {
    session_id: sessionId,
    identity,
    markdown: "# First section\n\ninitial screen content",
    title: "Plan",
    mode: "show",
  });
  const doc = await pushPage({ type: "doc", path: "notes.md", name: "Notes" });
  workspace = doc.workspace;
  await pushPage({
    type: "diff",
    name: "Review diff",
    content: "diff --git a/demo.py b/demo.py\n--- a/demo.py\n+++ b/demo.py\n@@ -1 +1 @@\n-value = 'old'\n+value = 'new'\n",
  });
});

test.afterAll(async () => {
  if (daemon && daemon.exitCode === null) {
    daemon.kill("SIGTERM");
    await new Promise((resolve) => {
      const timer = setTimeout(() => {
        if (daemon.exitCode === null) daemon.kill("SIGKILL");
        resolve();
      }, 5_000);
      daemon.once("exit", () => { clearTimeout(timer); resolve(); });
    });
  }
  if (root) rmSync(root, { recursive: true, force: true });
});

test("live pages, diffs, findings, and asks round-trip in a real browser", async ({ page }) => {
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(String(error)));
  await page.goto(baseUrl + "/");
  await expect(page.locator(".statusline > .cmd-led")).toHaveClass(/on/);

  await page.evaluate(() => {
    const toast = document.createElement("div");
    toast.className = "toast-msg sticky";
    toast.textContent = "test error";
    const dismiss = document.createElement("button");
    dismiss.className = "toast-x";
    dismiss.type = "button";
    dismiss.setAttribute("aria-label", "dismiss notification");
    dismiss.textContent = "✕";
    dismiss.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      toast.remove();
    });
    toast.append(dismiss);
    document.body.append(toast);
  });
  await page.getByRole("button", { name: "dismiss notification" }).click();
  await expect(page.locator(".toast-msg", { hasText: "test error" })).toHaveCount(0);

  await page.locator(".page-row", { hasText: "Plan" }).click();
  await expect(page.locator(".view")).toContainText("First section");
  await json("/v1/bridge/screen", {
    session_id: sessionId,
    identity,
    markdown: "## Appended live\n\nsecond block",
    mode: "append",
  });
  await expect(page.locator(".view")).toContainText("Appended live");

  await page.locator(".page-row", { hasText: "Notes" }).click();
  await expect(page.locator(".view")).toContainText("old path content");
  writeFileSync(path.join(repo, "notes.md"), "# Notes\n\nnew path content\n");
  await page.locator(".page-row", { hasText: "Plan" }).click();
  await page.locator(".page-row", { hasText: "Notes" }).click();
  await expect(page.locator(".view")).toContainText("new path content");

  await page.locator(".page-row", { hasText: "Review diff" }).click();
  await expect(page.locator(".dfile-head")).toContainText("demo.py");
  await page.locator(".dfile-head").click();
  await page.locator(".drow.add").click();
  await page.locator(".annot-editor textarea").fill("Needs a regression test");
  await page.locator(".annot-editor .tbtn.primary").click();
  await page.locator(".ctab", { hasText: "annotations" }).click();
  await expect(page.locator(".ftext")).toContainText("Needs a regression test");

  const revised = await pushPage({
    type: "diff",
    name: "Review diff",
    content: "diff --git a/demo.py b/demo.py\n--- a/demo.py\n+++ b/demo.py\n@@ -1 +1,2 @@\n-value = 'old'\n+value = 'newer'\n+tested = True\n",
  });
  expect(revised.rev).toBe(2);
  await expect(page.locator(".fstate.stale")).toHaveText("stale r1→r2");

  await page.locator(".ctab", { hasText: "asks" }).click();
  await page.locator(".ask-input").fill("Does the agent see this?");
  await page.locator(".ask-input").press("Enter");
  await expect(page.locator(".ask-text")).toContainText("Does the agent see this?");
  const ledger = await json(`/v1/bridge/findings?session_id=${sessionId}`);
  const ask = ledger.asks.find((item) => item.text === "Does the agent see this?");
  expect(ask).toBeTruthy();
  await json("/v1/bridge/ask_reply", {
    session_id: sessionId,
    ask_id: ask.ask_id,
    markdown: "Yes — over the same review channel.",
  });
  await expect(page.locator(".ask-answer")).toContainText("same review channel");

  expect(workspace).toBeTruthy();
  expect(pageErrors).toEqual([]);
});
