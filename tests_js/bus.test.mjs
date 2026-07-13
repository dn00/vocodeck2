import assert from "node:assert/strict";
import test from "node:test";

import {
  connectBus,
  OutcomeUnknownError,
} from "../src/voco/server/static/bus.mjs";

class FakeSocket {
  static OPEN = 1;
  static instances = [];

  constructor() {
    this.readyState = FakeSocket.OPEN;
    this.sent = [];
    FakeSocket.instances.push(this);
    queueMicrotask(() => this.onopen?.());
  }

  send(value) { this.sent.push(JSON.parse(value)); }

  close() {
    this.readyState = 3;
    queueMicrotask(() => this.onclose?.());
  }

  snapshot() {
    this.onmessage?.({ data: JSON.stringify({
      type: "snapshot", payload: { sessions: [], workspaces: [] },
    }) });
  }
}

test("timed out mutation is sent once and blocks retry until snapshot", async () => {
  FakeSocket.instances = [];
  globalThis.WebSocket = FakeSocket;
  globalThis.window = { __VOCO__: {} };
  globalThis.location = { protocol: "http:", host: "localhost" };
  globalThis.sessionStorage = {
    getItem() { return null; },
    setItem() {},
  };
  globalThis.fetch = async () => ({ ok: false });

  const snapshots = [];
  const store = {
    connected: false,
    _notify() {},
    applyEvent() {},
    applySnapshot(value) { snapshots.push(value); },
  };
  const bus = connectBus(store, { commandTimeoutMs: 5 });
  await new Promise((resolve) => setTimeout(resolve, 0));
  const first = FakeSocket.instances[0];

  await assert.rejects(
    bus.command("session.spawn", { root: "/repo" }),
    (error) => error instanceof OutcomeUnknownError
      && error.code === "outcome_unknown",
  );
  assert.equal(first.sent.length, 1);
  await assert.rejects(
    bus.command("session.spawn", { root: "/repo" }),
    OutcomeUnknownError,
  );

  await new Promise((resolve) => setTimeout(resolve, 550));
  const second = FakeSocket.instances.at(-1);
  second.snapshot();
  assert.equal(snapshots.length, 1);
  const pending = bus.command("state.get", {});
  assert.equal(second.sent.length, 1);
  second.onmessage({ data: JSON.stringify({
    id: second.sent[0].id, ok: true, payload: { ok: true },
  }) });
  assert.deepEqual(await pending, { ok: true });
  assert.equal(first.sent.length, 1);
});
