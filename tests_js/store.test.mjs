import assert from "node:assert/strict";
import test from "node:test";

import { Store } from "../src/voco/server/static/store.mjs";

test("live ledger events do not masquerade as a complete lazy fetch", () => {
  const store = new Store();
  store.applyEvent({ type: "finding.added", payload: {
    workspace: "ws", finding_id: "live", text: "new",
  } });
  assert.equal(store.loadedFindingWorkspaces.has("ws"), false);
  store.setFindingSnapshot("ws", [
    { finding_id: "old", text: "persisted" },
    { finding_id: "live", text: "stale server copy" },
  ]);
  assert.equal(store.loadedFindingWorkspaces.has("ws"), true);
  assert.deepEqual(store.findingsFor("ws").map((f) => f.finding_id).sort(),
    ["live", "old"]);
  assert.equal(store.findings.get("ws").get("live").text, "new");
});

test("ask lazy fetch merges under newer live answers", () => {
  const store = new Store();
  store.applyEvent({ type: "ask.answered", payload: {
    workspace: "ws", ask_id: "a1", created_ts: 1, answer: "live",
  } });
  store.setAskSnapshot("ws", [
    { ask_id: "a0", created_ts: 0, answer: null },
    { ask_id: "a1", created_ts: 1, answer: null },
  ]);
  assert.deepEqual(store.asksFor("ws").map((a) => a.ask_id), ["a0", "a1"]);
  assert.equal(store.asks.get("ws").get("a1").answer, "live");
});

test("queue count converges across enqueue and drain events", () => {
  const store = new Store();
  store.sessions.set("s1", { session_id: "s1", queued: 0 });
  store.applyEvent({ type: "input.queued", payload: {
    session_id: "s1", queued: 2,
  } });
  assert.equal(store.sessions.get("s1").queued, 2);
  store.applyEvent({ type: "input.drained", payload: { session_id: "s1" } });
  assert.equal(store.sessions.get("s1").queued, 0);
});

test("authoritative snapshots advance the page cache generation", () => {
  const store = new Store();
  assert.equal(store.snapshotEpoch, 0);
  store.applySnapshot({ sessions: [], workspaces: [] });
  store.applySnapshot({ sessions: [], workspaces: [] });
  assert.equal(store.snapshotEpoch, 2);
});
