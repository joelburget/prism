/** Extra traces beyond the public corpus, run with `node extra-tests.ts`. */
import { DomainError, evaluateRequest } from "./workflow.ts";

function evaluate(input: unknown): unknown {
  try {
    return { ok: true, result: evaluateRequest(input) };
  } catch (error) {
    if (error instanceof DomainError)
      return { ok: false, error: { code: error.code } };
    throw error;
  }
}
let failures = 0;
function check(name: string, input: unknown, expected: unknown): void {
  const actual = JSON.stringify(evaluate(input));
  const wanted = JSON.stringify(expected);
  if (actual === wanted) return;
  failures += 1;
  console.error(`FAIL ${name}\n  expected ${wanted}\n  actual   ${actual}`);
}
const step = (id: string, needs: string[], extra = {}) => ({
  id,
  needs,
  amount: 10,
  ...extra,
});
const error = (code: string) => ({ ok: false, error: { code } });

check(
  "cancelled run IDs are not reusable",
  {
    steps: [step("a", [])],
    commands: [
      { op: "start", run: "r" },
      { op: "cancel", run: "r" },
      { op: "start", run: "r" },
    ],
  },
  error("DUPLICATE_RUN"),
);
check(
  "crashing while down is an error",
  { steps: [step("a", [])], commands: [{ op: "crash" }, { op: "crash" }] },
  error("PROCESS_DOWN"),
);
check(
  "self dependencies are cycles",
  { steps: [step("a", ["a"])], commands: [] },
  error("DEPENDENCY_CYCLE"),
);
check(
  "an explicit null checkpoint is not a valid name",
  { steps: [step("a", [])], commands: [{ op: "tick", crash_at: null }] },
  error("INVALID_INPUT"),
);
check(
  "runs keep independent service failure counters",
  {
    steps: [step("a", [], { failures: 1 })],
    commands: [
      { op: "start", run: "r1" },
      { op: "start", run: "r2" },
      { op: "tick" },
      { op: "tick" },
    ],
    max_attempts: 1,
  },
  {
    ok: true,
    result: {
      observations: [],
      final: {
        now: 0,
        up: true,
        runs: ["r1", "r2"].map((id) => ({
          id,
          status: "failed",
          cancel_requested: false,
          steps: [{ id: "a", status: "failed", attempts: 1, ready_at: 0 }],
        })),
        calls: ["r1", "r2"].map((id) => ({
          kind: "execute",
          key: [id, "a"],
          attempt: 1,
          outcome: "transient",
        })),
        effects: [],
      },
    },
  },
);
check(
  "repeated cancellation of a cancelling run is harmless",
  {
    steps: [step("a", [])],
    commands: [
      { op: "start", run: "r" },
      { op: "tick", crash_at: "after_begin" },
      { op: "restart" },
      { op: "cancel", run: "r" },
      { op: "cancel", run: "r" },
      { op: "tick" },
      { op: "cancel", run: "r" },
      { op: "tick" },
    ],
  },
  {
    ok: true,
    result: {
      observations: [],
      final: {
        now: 0,
        up: true,
        runs: [
          {
            id: "r",
            status: "cancelled",
            cancel_requested: true,
            steps: [{ id: "a", status: "cancelled", attempts: 1, ready_at: 0 }],
          },
        ],
        calls: [
          { kind: "lookup", key: ["r", "a"], attempt: 1, outcome: "missing" },
        ],
        effects: [],
      },
    },
  },
);
check(
  "a failed run blocks independent pending branches only in that run",
  {
    steps: [step("a", [], { failures: 5 }), step("b", [])],
    commands: [
      { op: "start", run: "r" },
      { op: "tick" },
      { op: "advance", by: 2 },
      { op: "tick" },
      { op: "advance", by: 2 },
      { op: "tick" },
      { op: "tick" },
    ],
  },
  {
    ok: true,
    result: {
      observations: [],
      final: {
        now: 4,
        up: true,
        runs: [
          {
            id: "r",
            status: "failed",
            cancel_requested: false,
            steps: [
              { id: "a", status: "failed", attempts: 3, ready_at: 4 },
              { id: "b", status: "blocked", attempts: 0, ready_at: 0 },
            ],
          },
        ],
        calls: [1, 2, 3].map((attempt) => ({
          kind: "execute",
          key: ["r", "a"],
          attempt,
          outcome: "transient",
        })),
        effects: [],
      },
    },
  },
);

/* Leased-worker traces for checkpoint two. `trace` keeps expectations compact
 * by projecting the command results and the durable/audit parts of FINAL. */
function trace(input: unknown): unknown {
  const outcome = evaluate(input) as {
    ok: boolean;
    result?: { results: unknown[]; final: { runs: unknown; calls: unknown; effects: unknown } };
  };
  if (!outcome.ok || !outcome.result) return outcome;
  const { results, final } = outcome.result;
  return { results, runs: final.runs, calls: final.calls, effects: final.effects };
}
function checkTrace(name: string, input: unknown, expected: unknown): void {
  const actual = JSON.stringify(trace(input));
  const wanted = JSON.stringify(expected);
  if (actual === wanted) return;
  failures += 1;
  console.error(`FAIL ${name}\n  expected ${wanted}\n  actual   ${actual}`);
}
const worked = (
  id: string,
  status: string,
  attempts: number,
  ready_at: number,
  lease: unknown = null,
) => ({ id, status, attempts, ready_at, lease });
const leaseOf = (worker: string, ticket: number, expires: number) => ({
  worker,
  ticket,
  expires,
});
const run1 = (status: string, cancel_requested: boolean, steps: unknown[]) => [
  { id: "r", status, cancel_requested, steps },
]; 
const executed = (
  worker: string,
  ticket: number,
  step: string,
  attempt: number,
  outcome: string,
) => ({ worker, ticket, kind: "execute", key: ["r", step], attempt, outcome });
const probed = (
  worker: string,
  ticket: number,
  step: string,
  attempt: number,
  outcome: string,
) => ({ worker, ticket, kind: "lookup", key: ["r", step], attempt, outcome });

check(
  "worker existence precedes availability",
  {
    steps: [step("s", [])],
    workers: ["a"],
    commands: [{ op: "crash", worker: "a" }, { op: "claim", worker: "zz" }],
  },
  error("UNKNOWN_WORKER"),
);
check(
  "availability precedes the busy check",
  {
    steps: [step("s", [])],
    workers: ["a"],
    commands: [
      { op: "start", run: "r" },
      { op: "claim", worker: "a" },
      { op: "crash", worker: "a" },
      { op: "claim", worker: "a" },
    ],
  },
  error("WORKER_DOWN"),
);
check(
  "ticket existence precedes ownership",
  {
    steps: [step("s", [])],
    workers: ["a", "b"],
    commands: [{ op: "call", worker: "a", ticket: 9 }],
  },
  error("UNKNOWN_TICKET"),
);
check(
  "ownership stays with the original worker after a reclaim",
  {
    steps: [step("s", [])],
    workers: ["a", "b"],
    commands: [
      { op: "start", run: "r" },
      { op: "claim", worker: "a" },
      { op: "advance", by: 5 },
      { op: "claim", worker: "b" },
      { op: "call", worker: "b", ticket: 1 },
    ],
  },
  error("WRONG_WORKER"),
);
check(
  "deliver validates the ticket",
  {
    steps: [step("s", [])],
    workers: ["a"],
    commands: [{ op: "deliver", ticket: 1 }],
  },
  error("UNKNOWN_TICKET"),
);
check(
  "restarting an up worker is an error",
  {
    steps: [step("s", [])],
    workers: ["a"],
    commands: [{ op: "restart", worker: "a" }],
  },
  error("WORKER_UP"),
);
check(
  "checkpoint-one ticks are not leased commands",
  { steps: [step("s", [])], workers: ["a"], commands: [{ op: "tick" }] },
  error("INVALID_INPUT"),
);
check(
  "the command bound is an input error",
  {
    steps: [step("s", [])],
    workers: ["a"],
    commands: Array.from({ length: 2001 }, () => ({ op: "observe" })),
  },
  error("INVALID_INPUT"),
);
check(
  "a lease may not expire past the time bound",
  {
    steps: [step("s", [])],
    workers: ["a"],
    lease_duration: 1_000_000,
    commands: [
      { op: "start", run: "r" },
      { op: "advance", by: 2_147_483_647 },
      { op: "claim", worker: "a" },
    ],
  },
  error("TIME_OVERFLOW"),
);
check(
  "renewal may not expire past the time bound",
  {
    steps: [step("s", [])],
    workers: ["a"],
    lease_duration: 1_000_000,
    commands: [
      { op: "start", run: "r" },
      { op: "advance", by: 2_146_483_647 },
      { op: "claim", worker: "a" },
      { op: "advance", by: 516_353 },
      { op: "renew", worker: "a", ticket: 1 },
    ],
  },
  error("TIME_OVERFLOW"),
);
checkTrace(
  "repeated cancellation does not strand a fresh lookup lease",
  {
    steps: [step("s", [])],
    workers: ["a", "b"],
    commands: [
      { op: "start", run: "r" },
      { op: "claim", worker: "a" },
      { op: "cancel", run: "r" },
      { op: "claim", worker: "b" },
      { op: "cancel", run: "r" },
      { op: "call", worker: "b", ticket: 2 },
      { op: "deliver", ticket: 2 },
    ],
  },
  {
    results: [
      null,
      { ticket: 1 },
      null,
      { ticket: 2 },
      null,
      { kind: "lookup", outcome: "missing" },
      { committed: true },
    ],
    runs: run1("cancelled", true, [worked("s", "cancelled", 1, 0)]),
    calls: [probed("b", 2, "s", 1, "missing")],
    effects: [],
  },
);
checkTrace(
  "an expired lease leaves its worker free and is recovered before new work",
  {
    steps: [step("x", []), step("y", [])],
    workers: ["a"],
    commands: [
      { op: "start", run: "r" },
      { op: "claim", worker: "a" },
      { op: "advance", by: 5 },
      { op: "claim", worker: "a" },
    ],
  },
  {
    results: [null, { ticket: 1 }, null, { ticket: 2 }],
    runs: run1("active", false, [
      worked("x", "running", 1, 0, leaseOf("a", 2, 10)),
      worked("y", "pending", 0, 0),
    ]),
    calls: [],
    effects: [],
  },
);
checkTrace(
  "a superseded ticket cannot be renewed",
  {
    steps: [step("s", [])],
    workers: ["a", "b"],
    commands: [
      { op: "start", run: "r" },
      { op: "claim", worker: "a" },
      { op: "advance", by: 5 },
      { op: "claim", worker: "b" },
      { op: "renew", worker: "a", ticket: 1 },
    ],
  },
  {
    results: [null, { ticket: 1 }, null, { ticket: 2 }, { renewed: false }],
    runs: run1("active", false, [
      worked("s", "running", 1, 0, leaseOf("b", 2, 10)),
    ]),
    calls: [],
    effects: [],
  },
);
checkTrace(
  "a cancelled run keeps every effect its lookups find",
  {
    steps: [step("x", []), step("y", [])],
    workers: ["a", "b"],
    commands: [
      { op: "start", run: "r" },
      { op: "claim", worker: "a" },
      { op: "call", worker: "a", ticket: 1 },
      { op: "deliver", ticket: 1 },
      { op: "claim", worker: "a" },
      { op: "call", worker: "a", ticket: 2 },
      { op: "cancel", run: "r" },
      { op: "claim", worker: "b" },
      { op: "call", worker: "b", ticket: 3 },
      { op: "deliver", ticket: 3 },
    ],
  },
  {
    results: [
      null,
      { ticket: 1 },
      { kind: "execute", outcome: "applied" },
      { committed: true },
      { ticket: 2 },
      { kind: "execute", outcome: "applied" },
      null,
      { ticket: 3 },
      { kind: "lookup", outcome: "found" },
      { committed: true },
    ],
    runs: run1("cancelled", true, [
      worked("x", "succeeded", 1, 0),
      worked("y", "succeeded", 1, 0),
    ]),
    calls: [
      executed("a", 1, "x", 1, "applied"),
      executed("a", 2, "y", 1, "applied"),
      probed("b", 3, "y", 1, "found"),
    ],
    effects: [
      { key: ["r", "x"], amount: 10 },
      { key: ["r", "y"], amount: 10 },
    ],
  },
);
checkTrace(
  "cancelling a failing run is a no-op, flag included",
  {
    steps: [step("x", [], { failures: 1 }), step("y", [])],
    workers: ["a", "b"],
    max_attempts: 1,
    commands: [
      { op: "start", run: "r" },
      { op: "claim", worker: "a" },
      { op: "claim", worker: "b" },
      { op: "call", worker: "a", ticket: 1 },
      { op: "deliver", ticket: 1 },
      { op: "cancel", run: "r" },
    ],
  },
  {
    results: [
      null,
      { ticket: 1 },
      { ticket: 2 },
      { kind: "execute", outcome: "transient" },
      { committed: true },
      null,
    ],
    runs: run1("failing", false, [
      worked("x", "failed", 1, 0),
      worked("y", "running", 1, 0, leaseOf("b", 2, 0)),
    ]),
    calls: [executed("a", 1, "x", 1, "transient")],
    effects: [],
  },
);
checkTrace(
  "a stale transient cannot rewind a replacement's committed success",
  {
    steps: [step("s", [], { failures: 1 })],
    workers: ["a", "b"],
    commands: [
      { op: "start", run: "r" },
      { op: "claim", worker: "a" },
      { op: "call", worker: "a", ticket: 1 },
      { op: "advance", by: 5 },
      { op: "claim", worker: "b" },
      { op: "call", worker: "b", ticket: 2 },
      { op: "deliver", ticket: 1 },
      { op: "deliver", ticket: 2 },
    ],
  },
  {
    results: [
      null,
      { ticket: 1 },
      { kind: "execute", outcome: "transient" },
      null,
      { ticket: 2 },
      { kind: "execute", outcome: "applied" },
      { committed: false },
      { committed: true },
    ],
    runs: run1("succeeded", false, [worked("s", "succeeded", 1, 0)]),
    calls: [
      executed("a", 1, "s", 1, "transient"),
      executed("b", 2, "s", 1, "applied"),
    ],
    effects: [{ key: ["r", "s"], amount: 10 }],
  },
);
console.log(failures === 0 ? "extra tests passed" : `${failures} failed`);
if (failures !== 0) process.exitCode = 1;
