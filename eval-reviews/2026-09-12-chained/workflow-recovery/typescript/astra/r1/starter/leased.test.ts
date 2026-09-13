import assert from "node:assert/strict";
import test from "node:test";
import { LeasedSimulator, parseLeasedWorkflow } from "./leased.ts";

function simulate(commands: unknown[], steps: unknown[] = [{ id: "s", needs: [], amount: 7 }], config = {}) {
  return new LeasedSimulator(parseLeasedWorkflow({
    workers: ["a", "b", "c"], steps, commands, ...config,
  })).run();
}
const start = { op: "start", run: "r" };
const claim = (worker: string) => ({ op: "claim", worker });
const call = (worker: string, ticket: number) => ({ op: "call", worker, ticket });
const deliver = (ticket: number) => ({ op: "deliver", ticket });
const advance = (by: number) => ({ op: "advance", by });
const cancel = { op: "cancel", run: "r" };

test("repeated cancellation preserves lookup lease and earlier observations", () => {
  const result = simulate([start, claim("a"), call("a", 1), cancel,
    claim("b"), { op: "observe" }, cancel, call("b", 2), deliver(1), deliver(2)]);
  assert.deepEqual(result.results[8], { committed: false });
  assert.deepEqual(result.results[9], { committed: true });
  assert.equal(result.final.runs[0]!.status, "cancelled");
  assert.equal(result.final.runs[0]!.steps[0]!.status, "succeeded");
  assert.equal(result.final.calls.length, 2);
  assert.equal(result.final.effects.length, 1);
  const observation = result.results[5] as typeof result.final;
  assert.equal(observation.runs[0]!.steps[0]!.lease!.ticket, 2);
  assert.equal(observation.runs[0]!.steps[0]!.status, "running");
});

test("saved response survives worker crash, repeated calls and duplicate delivery", () => {
  const result = simulate([start, claim("a"), call("a", 1),
    { op: "crash", worker: "a" }, deliver(1), { op: "restart", worker: "a" },
    call("a", 1), deliver(1), deliver(1)]);
  assert.deepEqual(result.results[4], { committed: false });
  assert.deepEqual(result.results[7], { committed: true });
  assert.deepEqual(result.results[8], { committed: false });
  assert.equal(result.final.calls.length, 1);
  assert.equal(result.final.runs[0]!.status, "succeeded");
});

test("failure draining fences parallel work and reconciles found and missing effects", () => {
  const steps = [
    { id: "failure", needs: [], amount: 1, failures: 1 },
    { id: "applied", needs: [], amount: 2 },
    { id: "missing", needs: [], amount: 3 },
    { id: "pending", needs: [], amount: 4 },
  ];
  const result = simulate([start, claim("a"), claim("b"), claim("c"),
    call("b", 2), call("a", 1), deliver(1), cancel, deliver(2), call("c", 3),
    claim("a"), call("a", 4), deliver(4), claim("b"), call("b", 5), deliver(5)],
    steps, { max_attempts: 1 });
  assert.deepEqual(result.results[8], { committed: false });
  assert.deepEqual(result.results[9], { outcome: "stale" });
  const run = result.final.runs[0]!;
  assert.equal(run.status, "failed");
  assert.equal(run.cancel_requested, false);
  assert.deepEqual(run.steps.map(step => step.status), ["failed", "succeeded", "blocked", "blocked"]);
  assert.ok(run.steps.every(step => step.lease === null));
  assert.deepEqual(result.final.calls.map(call => call.outcome), ["applied", "transient", "found", "missing"]);
  assert.equal(result.final.effects.length, 1);
});

test("expired transient cannot reschedule reclaimed attempt", () => {
  const result = simulate([start, claim("a"), call("a", 1), advance(5), claim("b"),
    deliver(1), call("a", 1), call("b", 2), deliver(2)],
    [{ id: "s", needs: [], amount: 7, failures: 1 }]);
  assert.deepEqual(result.results[5], { committed: false });
  assert.deepEqual(result.results[6], { outcome: "stale" });
  assert.deepEqual(result.final.calls.map(call => call.attempt), [1, 1]);
  assert.equal(result.final.runs[0]!.steps[0]!.ready_at, 0);
  assert.equal(result.final.runs[0]!.status, "succeeded");
});

test("worker validation precedes ticket errors and command validation precedes execution", () => {
  for (const [commands, code] of [
    [[call("unknown", 1)], "UNKNOWN_WORKER"],
    [[{ op: "crash", worker: "a" }, call("a", 1)], "WORKER_DOWN"],
    [[start, claim("a"), call("b", 1)], "WRONG_WORKER"],
    [[call("a", 1)], "UNKNOWN_TICKET"],
    [[call("unknown", 1), { op: "tick" }], "INVALID_INPUT"],
  ] as const) {
    assert.throws(() => simulate([...commands]), { code });
  }
});
