/** Extra traces beyond the public corpus, run with `node extra-tests.ts`. */
import { DomainError, parseWorkflow, Simulator } from "./workflow.ts";

function evaluate(input: unknown): unknown {
  try {
    return { ok: true, result: new Simulator(parseWorkflow(input)).run() };
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
console.log(failures === 0 ? "extra tests passed" : `${failures} failed`);
if (failures !== 0) process.exitCode = 1;
