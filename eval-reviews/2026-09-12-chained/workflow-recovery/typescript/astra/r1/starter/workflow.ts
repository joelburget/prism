/** Deterministic durable DAG runner and independently persistent mock service. */
export const TIME_LIMIT = 2_147_483_647;
export class DomainError extends Error {
  readonly code: string;
  constructor(code: string) {
    super(code);
    this.code = code;
  }
}
function require(
  condition: unknown,
  code = "INVALID_INPUT",
): asserts condition {
  if (!condition) throw new DomainError(code);
}
function fields(
  value: unknown,
  required: string[],
  optional: string[] = [],
): Record<string, unknown> {
  require(value !== null && typeof value === "object" && !Array.isArray(value));
  const obj = value as Record<string, unknown>;
  require(required.every((key) => Object.hasOwn(obj, key)));
  require(
    Object.keys(obj).every(
      (key) => required.includes(key) || optional.includes(key),
    ),
  );
  return obj;
}
function integer(value: unknown, low: number, high: number): number {
  require(
    typeof value === "number" &&
      Number.isInteger(value) &&
      value >= low &&
      value <= high,
  );
  return value;
}
function identifier(value: unknown): string {
  require(
    typeof value === "string" &&
      value.length >= 1 &&
      value.length <= 64 &&
      !/[^A-Za-z0-9_-]/.test(value),
  );
  return value;
}
export interface StepDefinition {
  readonly id: string;
  readonly needs: readonly string[];
  readonly amount: number;
  readonly failures: number;
}
export type Command =
  | { op: "start" | "cancel"; run: string }
  | { op: "advance"; by: number }
  | { op: "tick"; crashAt: "after_begin" | "after_call" | null }
  | { op: "observe" | "crash" | "restart" };
export interface Workflow {
  readonly steps: readonly StepDefinition[];
  readonly commands: readonly Command[];
  readonly maxAttempts: number;
  readonly retryDelay: number;
}
function parseStep(raw: unknown): StepDefinition {
  const obj = fields(raw, ["id", "needs", "amount"], ["failures"]);
  const id = identifier(obj.id);
  require(Array.isArray(obj.needs));
  const needs = obj.needs.map(identifier);
  require(new Set(needs).size === needs.length);
  return {
    id,
    needs,
    amount: integer(obj.amount, 1, 1_000_000),
    failures: integer(
      Object.hasOwn(obj, "failures") ? obj.failures : 0,
      0,
      100,
    ),
  };
}
function parseCommand(raw: unknown): Command {
  require(raw !== null && typeof raw === "object" && !Array.isArray(raw));
  const obj = raw as Record<string, unknown>;
  switch (obj.op) {
    case "start":
    case "cancel":
      fields(raw, ["op", "run"]);
      return { op: obj.op, run: identifier(obj.run) };
    case "advance":
      fields(raw, ["op", "by"]);
      return { op: obj.op, by: integer(obj.by, 0, TIME_LIMIT) };
    case "tick": {
      fields(raw, ["op"], ["crash_at"]);
      const crashAt = Object.hasOwn(obj, "crash_at") ? obj.crash_at : null;
      require(
        !Object.hasOwn(obj, "crash_at") ||
          crashAt === "after_begin" ||
          crashAt === "after_call",
      );
      return {
        op: obj.op,
        crashAt: crashAt as "after_begin" | "after_call" | null,
      };
    }
    case "observe":
    case "crash":
    case "restart":
      fields(raw, ["op"]);
      return { op: obj.op };
    default:
      throw new DomainError("INVALID_INPUT");
  }
}
function validateGraph(steps: readonly StepDefinition[]): void {
  const ids = new Set(steps.map((step) => step.id));
  require(ids.size === steps.length, "DUPLICATE_STEP");
  require(steps.every((step) =>
    step.needs.every((dep) => ids.has(dep)),
  ), "UNKNOWN_DEPENDENCY");
  let remaining = [...steps];
  const visited = new Set<string>();
  while (remaining.length) {
    const ready = remaining.filter((step) =>
      step.needs.every((dep) => visited.has(dep)),
    );
    require(ready.length > 0, "DEPENDENCY_CYCLE");
    ready.forEach((step) => visited.add(step.id));
    remaining = remaining.filter((step) => !visited.has(step.id));
  }
}
export function parseWorkflow(raw: unknown): Workflow {
  const obj = fields(
    raw,
    ["steps", "commands"],
    ["max_attempts", "retry_delay"],
  );
  require(
    Array.isArray(obj.steps) &&
      obj.steps.length > 0 &&
      Array.isArray(obj.commands),
  );
  const workflow: Workflow = {
    steps: obj.steps.map(parseStep),
    commands: obj.commands.map(parseCommand),
    maxAttempts: integer(
      Object.hasOwn(obj, "max_attempts") ? obj.max_attempts : 3,
      1,
      10,
    ),
    retryDelay: integer(
      Object.hasOwn(obj, "retry_delay") ? obj.retry_delay : 2,
      1,
      1_000_000,
    ),
  };
  validateGraph(workflow.steps); // Scalar and shape validation has finished before graph checks.
  return workflow;
}
export interface StepState {
  id: string;
  status: "pending" | "running" | "succeeded" | "failed" | "blocked" | "cancelled";
  attempts: number;
  ready_at: number;
}
export interface RunState {
  id: string;
  status: "active" | "succeeded" | "failed" | "cancelling" | "cancelled";
  cancel_requested: boolean;
  steps: StepState[];
}
type ActionKey = readonly [string, string];
type ExecuteOutcome = "applied" | "replayed" | "transient";
interface ServiceCall {
  kind: "execute" | "lookup";
  key: ActionKey;
  attempt: number;
  outcome: ExecuteOutcome | "found" | "missing";
}
interface Effect {
  key: ActionKey;
  amount: number;
}
export class MockService {
  readonly calls: ServiceCall[] = [];
  readonly effects: Effect[] = [];
  private readonly applied = new Set<string>();
  private readonly failuresSeen = new Map<string, number>();
  execute(key: ActionKey, amount: number, attempt: number, failures = 0): ExecuteOutcome {
    const encoded = JSON.stringify(key);
    const seen = this.failuresSeen.get(encoded) ?? 0;
    let outcome: ExecuteOutcome;
    if (this.applied.has(encoded)) {
      outcome = "replayed";
    } else if (seen < failures) {
      this.failuresSeen.set(encoded, seen + 1);
      outcome = "transient";
    } else {
      this.applied.add(encoded);
      this.effects.push({ key, amount });
      outcome = "applied";
    }
    this.calls.push({ kind: "execute", key, attempt, outcome });
    return outcome;
  }
  lookup(key: ActionKey, attempt: number): "found" | "missing" {
    const outcome = this.applied.has(JSON.stringify(key)) ? "found" : "missing";
    this.calls.push({ kind: "lookup", key, attempt, outcome });
    return outcome;
  }
}
export interface Snapshot {
  now: number;
  up: boolean;
  runs: RunState[];
}
export interface SimulationResult {
  observations: Snapshot[];
  final: Snapshot & { calls: ServiceCall[]; effects: Effect[] };
}
interface Action {
  run: RunState;
  step: StepState;
  definition: StepDefinition;
}
export class Simulator {
  readonly workflow: Workflow;
  now = 0;
  up = true;
  readonly runs: RunState[] = [];
  readonly service = new MockService();
  readonly observations: Snapshot[] = [];
  constructor(workflow: Workflow) {
    this.workflow = workflow;
  }
  selectAction(): Action | undefined {
    for (const run of this.runs) {
      const index = run.steps.findIndex((step) => step.status === "running");
      if (index !== -1)
        return {
          run,
          step: run.steps[index]!,
          definition: this.workflow.steps[index]!,
        };
    }
    for (const run of this.runs) {
      if (run.status !== "active") continue;
      const succeeded = new Set(
        run.steps
          .filter((step) => step.status === "succeeded")
          .map((step) => step.id),
      );
      for (const [index, step] of run.steps.entries()) {
        const definition = this.workflow.steps[index]!;
        if (
          step.status === "pending" &&
          step.ready_at <= this.now &&
          definition.needs.every((dep) => succeeded.has(dep))
        )
          return { run, step, definition };
      }
    }
    return undefined;
  }
  tick(crashAt: "after_begin" | "after_call" | null = null): void {
    const action = this.selectAction();
    if (!action) return;
    const { run, step, definition } = action;
    if (step.status === "pending") {
      step.status = "running";
      step.attempts += 1;
    }
    if (crashAt === "after_begin") {
      this.up = false;
      return;
    }
    // Responses are volatile until the checkpoint has passed. Recovery always
    // consults the service again using the durable attempt and structured key.
    const outcome = run.status === "cancelling"
      ? this.service.lookup([run.id, step.id], step.attempts)
      : this.service.execute([run.id, step.id], definition.amount, step.attempts, definition.failures);
    if (crashAt === "after_call") {
      this.up = false;
      return;
    }
    if (run.status === "cancelling") {
      step.status = outcome === "found" ? "succeeded" : "cancelled";
      run.status = "cancelled";
      return;
    }
    if (outcome === "transient") {
      if (step.attempts < this.workflow.maxAttempts) {
        const deadline = this.now + this.workflow.retryDelay;
        require(deadline <= TIME_LIMIT, "TIME_OVERFLOW");
        step.status = "pending";
        step.ready_at = deadline;
      } else {
        step.status = "failed";
        run.status = "failed";
        for (const other of run.steps)
          if (other.status === "pending") other.status = "blocked";
      }
      return;
    }
    step.status = "succeeded";
    if (run.steps.every((state) => state.status === "succeeded"))
      run.status = "succeeded";
  }
  apply(command: Command): void {
    if (command.op !== "observe" && command.op !== "advance" && command.op !== "restart")
      require(this.up, "PROCESS_DOWN");
    switch (command.op) {
      case "start":
        require(this.runs.every(
          (run) => run.id !== command.run,
        ), "DUPLICATE_RUN");
        this.runs.push({
          id: command.run,
          status: "active",
          cancel_requested: false,
          steps: this.workflow.steps.map((step) => ({
            id: step.id,
            status: "pending",
            attempts: 0,
            ready_at: this.now,
          })),
        });
        break;
      case "tick":
        this.tick(command.crashAt);
        break;
      case "crash":
        this.up = false;
        break;
      case "restart":
        require(!this.up, "PROCESS_UP");
        this.up = true;
        break;
      case "cancel": {
        const run = this.runs.find((run) => run.id === command.run);
        require(run, "UNKNOWN_RUN");
        if (run.status !== "active" && run.status !== "cancelling") break;
        run.cancel_requested = true;
        for (const step of run.steps)
          if (step.status === "pending") step.status = "cancelled";
        run.status = run.steps.some((step) => step.status === "running")
          ? "cancelling" : "cancelled";
        break;
      }
      case "advance":
        require(this.now + command.by <= TIME_LIMIT, "TIME_OVERFLOW");
        this.now += command.by;
        break;
      case "observe":
        this.observations.push(this.snapshot());
        break;
    }
  }
  snapshot(): Snapshot {
    return {
      now: this.now,
      up: this.up,
      runs: this.runs.map((run) => ({
        ...run,
        steps: run.steps.map((step) => ({ ...step })),
      })),
    };
  }
  run(): SimulationResult {
    this.workflow.commands.forEach((command) => this.apply(command));
    return {
      observations: this.observations,
      final: {
        ...this.snapshot(),
        calls: this.service.calls,
        effects: this.service.effects,
      },
    };
  }
}
