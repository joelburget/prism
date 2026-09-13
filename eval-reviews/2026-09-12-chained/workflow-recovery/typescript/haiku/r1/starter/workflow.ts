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

export type WorkerCommand =
  | { op: "start"; run: string }
  | { op: "advance"; by: number }
  | { op: "observe" }
  | { op: "crash"; worker: string }
  | { op: "restart"; worker: string }
  | { op: "claim"; worker: string }
  | { op: "renew"; worker: string; ticket: number }
  | { op: "call"; worker: string; ticket: number }
  | { op: "deliver"; ticket: number }
  | { op: "cancel"; run: string };

export interface Workflow {
  readonly steps: readonly StepDefinition[];
  readonly commands: readonly Command[];
  readonly maxAttempts: number;
  readonly retryDelay: number;
}

export interface WorkerWorkflow {
  readonly steps: readonly StepDefinition[];
  readonly commands: readonly WorkerCommand[];
  readonly workers: readonly string[];
  readonly maxAttempts: number;
  readonly retryDelay: number;
  readonly leaseDuration: number;
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

function parseWorkerCommand(raw: unknown): WorkerCommand {
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
    case "observe":
      fields(raw, ["op"]);
      return { op: obj.op };
    case "crash":
    case "restart":
      fields(raw, ["op", "worker"]);
      return { op: obj.op, worker: identifier(obj.worker) };
    case "claim":
      fields(raw, ["op", "worker"]);
      return { op: obj.op, worker: identifier(obj.worker) };
    case "renew":
      fields(raw, ["op", "worker", "ticket"]);
      return {
        op: obj.op,
        worker: identifier(obj.worker),
        ticket: integer(obj.ticket, 1, TIME_LIMIT),
      };
    case "call":
      fields(raw, ["op", "worker", "ticket"]);
      return {
        op: obj.op,
        worker: identifier(obj.worker),
        ticket: integer(obj.ticket, 1, TIME_LIMIT),
      };
    case "deliver":
      fields(raw, ["op", "ticket"]);
      return { op: obj.op, ticket: integer(obj.ticket, 1, TIME_LIMIT) };
    default:
      throw new DomainError("INVALID_INPUT");
  }
}

function validateGraph(steps: readonly StepDefinition[]): void {
  const ids = new Set(steps.map((step) => step.id));
  require(ids.size === steps.length, "DUPLICATE_STEP");
  require(
    steps.every((step) => step.needs.every((dep) => ids.has(dep))),
    "UNKNOWN_DEPENDENCY",
  );
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
  const obj = fields(raw, ["steps", "commands"], ["max_attempts", "retry_delay"]);
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
  validateGraph(workflow.steps);
  return workflow;
}

export function parseWorkerWorkflow(raw: unknown): WorkerWorkflow {
  const obj = fields(raw, ["steps", "commands", "workers"], [
    "max_attempts",
    "retry_delay",
    "lease_duration",
  ]);
  require(
    Array.isArray(obj.steps) &&
      obj.steps.length > 0 &&
      Array.isArray(obj.commands) &&
      Array.isArray(obj.workers) &&
      obj.workers.length > 0 &&
      obj.workers.length <= 100,
  );
  const workerIds = obj.workers.map(identifier);
  require(new Set(workerIds).size === workerIds.length);

  const workflow: WorkerWorkflow = {
    steps: obj.steps.map(parseStep),
    commands: obj.commands.map(parseWorkerCommand),
    workers: workerIds,
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
    leaseDuration: integer(
      Object.hasOwn(obj, "lease_duration") ? obj.lease_duration : 5,
      1,
      1_000_000,
    ),
  };
  validateGraph(workflow.steps);
  require(obj.commands.length <= 2000);
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
  status: "active" | "succeeded" | "failed" | "cancelling" | "cancelled" | "failing";
  cancel_requested: boolean;
  steps: StepState[];
}

export interface Lease {
  worker: string;
  ticket: number;
  expires: number;
}

export interface StepStateWithLease extends StepState {
  lease: Lease | null;
}

export interface RunStateWithLease extends Omit<RunState, "steps"> {
  steps: StepStateWithLease[];
}

export interface WorkerState {
  id: string;
  up: boolean;
}

type ActionKey = readonly [string, string];

interface ServiceCall {
  kind: "execute" | "lookup";
  key: ActionKey;
  attempt: number;
  outcome: "applied" | "replayed" | "transient" | "found" | "missing";
  worker?: string;
  ticket?: number;
}

interface Effect {
  key: ActionKey;
  amount: number;
}

export class MockService {
  readonly calls: ServiceCall[] = [];
  readonly effects: Effect[] = [];
  readonly failureCounters: Map<string, number> = new Map();
  readonly appliedKeys: Set<string> = new Set();

  execute(
    key: ActionKey,
    amount: number,
    attempt: number,
    failures: number,
  ): "applied" | "replayed" | "transient" {
    const keyStr = JSON.stringify(key);
    
    if (this.appliedKeys.has(keyStr)) {
      return "replayed";
    }

    const failureCount = this.failureCounters.get(keyStr) || 0;
    if (failureCount < failures) {
      this.failureCounters.set(keyStr, failureCount + 1);
      return "transient";
    }

    this.appliedKeys.add(keyStr);
    this.effects.push({ key, amount });
    return "applied";
  }

  lookup(key: ActionKey): "found" | "missing" {
    const keyStr = JSON.stringify(key);
    return this.appliedKeys.has(keyStr) ? "found" : "missing";
  }
}

export interface Snapshot {
  now: number;
  up: boolean;
  runs: RunState[];
}

export interface WorkerSnapshot {
  now: number;
  workers: WorkerState[];
  runs: RunStateWithLease[];
}

export interface SimulationResult {
  observations: Snapshot[];
  final: Snapshot & { calls: ServiceCall[]; effects: Effect[] };
}

export interface WorkerSimulationResult {
  results: unknown[];
  final: WorkerSnapshot & { calls: ServiceCall[]; effects: Effect[] };
}

interface Action {
  run: RunState;
  step: StepState;
  definition: StepDefinition;
  stepIndex: number;
}

interface SavedResponse {
  outcome: "applied" | "replayed" | "transient" | "found" | "missing";
  kind: "execute" | "lookup";
}

export class Simulator {
  readonly workflow: Workflow;
  now = 0;
  up = true;
  readonly runs: RunState[] = [];
  readonly service = new MockService();
  readonly observations: Snapshot[] = [];
  volatile = false;

  constructor(workflow: Workflow) {
    this.workflow = workflow;
  }

  selectAction(): Action | undefined {
    for (const run of this.runs) {
      for (const [index, step] of run.steps.entries()) {
        if (step.status === "running") {
          return {
            run,
            step,
            definition: this.workflow.steps[index]!,
            stepIndex: index,
          };
        }
      }
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
          return { run, step, definition, stepIndex: index };
      }
    }
    return undefined;
  }

  tick(crashAt: "after_begin" | "after_call" | null): void {
    require(this.up, "PROCESS_DOWN");

    const action = this.selectAction();
    if (!action) return;

    const { run, step, definition } = action;
    const isNewAttempt = step.status === "pending";

    if (isNewAttempt) {
      step.status = "running";
      step.attempts += 1;
    }

    // Checkpoint: after_begin
    if (crashAt === "after_begin") {
      this.up = false;
      this.volatile = true;
      return;
    }

    // Make service call
    let outcome: "applied" | "replayed" | "transient" | "found" | "missing";
    let kind: "execute" | "lookup";

    if (run.status === "cancelling" || run.status === "failing") {
      kind = "lookup";
      outcome = this.service.lookup([run.id, step.id]);
    } else {
      kind = "execute";
      outcome = this.service.execute(
        [run.id, step.id],
        definition.amount,
        step.attempts,
        definition.failures,
      );
    }

    this.service.calls.push({
      kind,
      key: [run.id, step.id],
      attempt: step.attempts,
      outcome,
    });

    // Checkpoint: after_call
    if (crashAt === "after_call") {
      this.up = false;
      this.volatile = true;
      return;
    }

    // Commit result
    if (kind === "execute") {
      if (outcome !== "transient") {
        step.status = "succeeded";
        if (run.steps.every((state) => state.status === "succeeded")) {
          run.status = "succeeded";
        }
      } else {
        if (step.attempts < this.workflow.maxAttempts) {
          step.status = "pending";
          const newReadyAt = this.now + this.workflow.retryDelay;
          require(newReadyAt <= TIME_LIMIT, "TIME_OVERFLOW");
          step.ready_at = newReadyAt;
        } else {
          step.status = "failed";
          run.status = "failed";
          for (const s of run.steps) {
            if (s.status === "pending") {
              s.status = "blocked";
            }
          }
        }
      }
    } else {
      if (outcome === "found") {
        step.status = "succeeded";
      } else {
        step.status = "cancelled";
      }
      if (run.steps.every((s) => s.status !== "running")) {
        run.status = "cancelled";
      }
    }
  }

  apply(command: Command): void {
    switch (command.op) {
      case "start":
        require(
          this.runs.every((run) => run.id !== command.run),
          "DUPLICATE_RUN",
        );
        this.runs.push({
          id: command.run,
          status: "active",
          cancel_requested: false,
          steps: this.workflow.steps.map((step) => ({
            id: step.id,
            status: "pending" as const,
            attempts: 0,
            ready_at: this.now,
          })),
        });
        break;
      case "tick":
        this.tick(command.crashAt);
        break;
      case "advance":
        require(this.now + command.by <= TIME_LIMIT, "TIME_OVERFLOW");
        this.now += command.by;
        break;
      case "observe":
        this.observations.push(this.snapshot());
        break;
      case "crash":
        this.up = false;
        this.volatile = true;
        break;
      case "restart":
        require(!this.up, "PROCESS_UP");
        this.up = true;
        this.volatile = false;
        break;
      case "cancel": {
        require(this.up, "PROCESS_DOWN");
        const run = this.runs.find((r) => r.id === command.run);
        require(run, "UNKNOWN_RUN");

        if (
          run.status === "succeeded" ||
          run.status === "failed" ||
          run.status === "cancelled"
        ) {
          return;
        }

        run.cancel_requested = true;
        for (const step of run.steps) {
          if (step.status === "pending") {
            step.status = "cancelled";
          }
        }

        const hasRunning = run.steps.some((s) => s.status === "running");
        if (!hasRunning) {
          run.status = "cancelled";
        } else {
          run.status = "cancelling";
        }
        break;
      }
      default:
        throw new DomainError("UNSUPPORTED_FEATURE");
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

export class WorkerSimulator {
  readonly workflow: WorkerWorkflow;
  now = 0;
  readonly workerStates: Map<string, boolean> = new Map();
  readonly runs: RunState[] = [];
  readonly service = new MockService();
  nextTicket = 1;
  readonly leases: Map<string, Lease | null> = new Map();
  readonly savedResponses: Map<number, SavedResponse> = new Map();
  readonly ticketToWorker: Map<number, string> = new Map();
  readonly ticketDelivered: Map<number, boolean> = new Map();
  readonly results: unknown[] = [];
  readonly observations: WorkerSnapshot[] = [];

  constructor(workflow: WorkerWorkflow) {
    this.workflow = workflow;
    for (const worker of workflow.workers) {
      this.workerStates.set(worker, true);
    }
  }

  selectClaimableStep(): { run: RunState; step: StepState; stepDef: StepDefinition; stepIndex: number } | undefined {
    for (const run of this.runs) {
      for (const [index, step] of run.steps.entries()) {
        const def = this.workflow.steps[index]!;
        if (step.status === "running") {
          const lease = this.leases.get(this.leaseKey(run.id, step.id));
          if (lease && this.now >= lease.expires) {
            return { run, step, stepDef: def, stepIndex: index };
          }
        }
      }
    }

    for (const run of this.runs) {
      if (run.status !== "active") continue;
      const succeeded = new Set(
        run.steps
          .filter((step) => step.status === "succeeded")
          .map((step) => step.id),
      );
      for (const [index, step] of run.steps.entries()) {
        const def = this.workflow.steps[index]!;
        if (
          step.status === "pending" &&
          step.ready_at <= this.now &&
          def.needs.every((dep) => succeeded.has(dep))
        ) {
          return { run, step, stepDef: def, stepIndex: index };
        }
      }
    }
    return undefined;
  }

  leaseKey(runId: string, stepId: string): string {
    return `${runId}:${stepId}`;
  }

  claim(worker: string): { ticket: number } | { ticket: null } {
    require(this.workerStates.has(worker), "UNKNOWN_WORKER");
    require(this.workerStates.get(worker), "WORKER_DOWN");

    const existingLease = Array.from(this.leases.values()).find(
      (lease) => lease && lease.worker === worker && this.now < lease.expires,
    );
    if (existingLease) {
      throw new DomainError("WORKER_BUSY");
    }

    const work = this.selectClaimableStep();
    if (!work) {
      return { ticket: null };
    }

    const { run, step, stepDef } = work;
    const ticket = this.nextTicket++;
    const expiresAt = this.now + this.workflow.leaseDuration;
    require(expiresAt <= TIME_LIMIT, "TIME_OVERFLOW");

    const leasingKey = this.leaseKey(run.id, step.id);
    const lease: Lease = { worker, ticket, expires: expiresAt };
    this.leases.set(leasingKey, lease);
    this.ticketToWorker.set(ticket, worker);

    const isRestart = step.status === "running";
    if (!isRestart) {
      step.status = "running";
      step.attempts += 1;
    }

    return { ticket };
  }

  call(worker: string, ticket: number): unknown {
    require(this.workerStates.has(worker), "UNKNOWN_WORKER");
    require(this.workerStates.get(worker), "WORKER_DOWN");
    require(this.ticketToWorker.has(ticket), "UNKNOWN_TICKET");
    require(this.ticketToWorker.get(ticket) === worker, "WRONG_WORKER");

    const leasedStep = this.findStepByTicket(ticket);
    if (!leasedStep) {
      throw new DomainError("UNKNOWN_TICKET");
    }

    const { run, step, stepDef } = leasedStep;
    const leasingKey = this.leaseKey(run.id, step.id);
    const lease = this.leases.get(leasingKey);

    if (!lease || lease.ticket !== ticket || this.now >= lease.expires) {
      return { outcome: "stale" };
    }

    if (this.savedResponses.has(ticket)) {
      const saved = this.savedResponses.get(ticket)!;
      return { kind: saved.kind, outcome: saved.outcome };
    }

    let outcome: "applied" | "replayed" | "transient" | "found" | "missing";
    let kind: "execute" | "lookup";

    if (run.status === "cancelling" || run.status === "failing") {
      kind = "lookup";
      outcome = this.service.lookup([run.id, step.id]);
    } else {
      kind = "execute";
      outcome = this.service.execute(
        [run.id, step.id],
        stepDef.amount,
        step.attempts,
        stepDef.failures,
      );
    }

    const response: SavedResponse = { outcome, kind };
    this.savedResponses.set(ticket, response);

    this.service.calls.push({
      worker,
      ticket,
      kind,
      key: [run.id, step.id],
      attempt: step.attempts,
      outcome,
    });

    return { kind, outcome };
  }

  deliver(ticket: number): { committed: boolean } {
    require(this.ticketToWorker.has(ticket), "UNKNOWN_TICKET");

    const leasedStep = this.findStepByTicket(ticket);
    if (!leasedStep) {
      return { committed: false };
    }

    const { run, step, stepDef } = leasedStep;
    const leasingKey = this.leaseKey(run.id, step.id);
    const lease = this.leases.get(leasingKey);
    const owner = this.ticketToWorker.get(ticket)!;
    const saved = this.savedResponses.get(ticket);

    if (!saved || !lease || lease.ticket !== ticket) {
      return { committed: false };
    }

    if (this.now >= lease.expires || !this.workerStates.get(owner)!) {
      return { committed: false };
    }

    if (this.ticketDelivered.has(ticket)) {
      return { committed: false };
    }

    this.ticketDelivered.set(ticket, true);

    if (saved.kind === "execute") {
      if (saved.outcome === "applied" || saved.outcome === "replayed") {
        step.status = "succeeded";
        if (run.steps.every((s) => s.status === "succeeded")) {
          run.status = "succeeded";
        }
      } else if (saved.outcome === "transient") {
        if (step.attempts < this.workflow.maxAttempts) {
          step.status = "pending";
          const newReadyAt = this.now + this.workflow.retryDelay;
          require(newReadyAt <= TIME_LIMIT, "TIME_OVERFLOW");
          step.ready_at = newReadyAt;
        } else {
          step.status = "failed";
          this.markFailureDrain(run);
        }
      }
    } else if (saved.kind === "lookup") {
      if (saved.outcome === "found") {
        step.status = "succeeded";
        if (run.steps.every((s) => s.status !== "running")) {
          if (run.status === "cancelling") {
            run.status = "cancelled";
          } else if (run.status === "failing") {
            run.status = "failed";
          }
        }
      } else {
        step.status = "cancelled";
        if (run.steps.every((s) => s.status !== "running")) {
          if (run.status === "cancelling") {
            run.status = "cancelled";
          }
        }
      }
    }

    this.leases.set(leasingKey, null);
    return { committed: true };
  }

  renew(worker: string, ticket: number): { renewed: boolean } {
    require(this.workerStates.has(worker), "UNKNOWN_WORKER");
    require(this.workerStates.get(worker), "WORKER_DOWN");
    require(this.ticketToWorker.has(ticket), "UNKNOWN_TICKET");
    require(this.ticketToWorker.get(ticket) === worker, "WRONG_WORKER");

    const leasedStep = this.findStepByTicket(ticket);
    if (!leasedStep) {
      return { renewed: false };
    }

    const { run, step } = leasedStep;
    const leasingKey = this.leaseKey(run.id, step.id);
    const lease = this.leases.get(leasingKey);

    if (!lease || lease.ticket !== ticket || this.now >= lease.expires) {
      return { renewed: false };
    }

    const newExpires = this.now + this.workflow.leaseDuration;
    require(newExpires <= TIME_LIMIT, "TIME_OVERFLOW");
    lease.expires = newExpires;
    return { renewed: true };
  }

  markFailureDrain(run: RunState): void {
    if (run.status !== "active" && run.status !== "failing") return;
    let hasRunning = false;
    for (const step of run.steps) {
      if (step.status === "running") {
        hasRunning = true;
        const leasingKey = this.leaseKey(run.id, step.id);
        const lease = this.leases.get(leasingKey);
        if (lease) {
          lease.expires = this.now;
        }
      } else if (step.status === "pending") {
        step.status = "blocked";
      }
    }
    run.status = hasRunning ? "failing" : "failed";
  }

  findStepByTicket(ticket: number): { run: RunState; step: StepState; stepDef: StepDefinition } | undefined {
    for (const run of this.runs) {
      for (const [index, step] of run.steps.entries()) {
        const leasingKey = this.leaseKey(run.id, step.id);
        const lease = this.leases.get(leasingKey);
        if (lease && lease.ticket === ticket) {
          return { run, step, stepDef: this.workflow.steps[index]! };
        }
      }
    }
    return undefined;
  }

  cancel(runId: string): void {
    const run = this.runs.find((r) => r.id === runId);
    require(run, "UNKNOWN_RUN");

    if (run.status === "succeeded" || run.status === "failed" || run.status === "cancelled" || run.status === "failing") {
      return;
    }

    run.cancel_requested = true;
    for (const step of run.steps) {
      if (step.status === "pending") {
        step.status = "cancelled";
      }
    }

    const hasRunning = run.steps.some((s) => s.status === "running");
    if (!hasRunning) {
      run.status = "cancelled";
    } else {
      run.status = "cancelling";
      for (const step of run.steps) {
        if (step.status === "running") {
          const leasingKey = this.leaseKey(run.id, step.id);
          const lease = this.leases.get(leasingKey);
          if (lease) {
            lease.expires = this.now;
          }
        }
      }
    }
  }

  apply(command: WorkerCommand): unknown {
    switch (command.op) {
      case "start":
        require(
          this.runs.every((run) => run.id !== command.run),
          "DUPLICATE_RUN",
        );
        const newRun: RunState = {
          id: command.run,
          status: "active",
          cancel_requested: false,
          steps: this.workflow.steps.map((step) => ({
            id: step.id,
            status: "pending" as const,
            attempts: 0,
            ready_at: this.now,
          })),
        };
        this.runs.push(newRun);
        for (const step of newRun.steps) {
          this.leases.set(this.leaseKey(command.run, step.id), null);
        }
        return null;

      case "advance":
        require(this.now + command.by <= TIME_LIMIT, "TIME_OVERFLOW");
        this.now += command.by;
        return null;

      case "observe": {
        const snapshot: WorkerSnapshot = {
          now: this.now,
          workers: this.workflow.workers.map((w) => ({
            id: w,
            up: this.workerStates.get(w)!,
          })),
          runs: this.runs.map((run) => ({
            ...run,
            steps: run.steps.map((step) => {
              const leasingKey = this.leaseKey(run.id, step.id);
              const lease = this.leases.get(leasingKey);
              return { ...step, lease: lease ?? null };
            }),
          })),
        };
        this.observations.push(snapshot);
        return snapshot;
      }

      case "crash":
        require(this.workerStates.has(command.worker), "UNKNOWN_WORKER");
        require(this.workerStates.get(command.worker), "WORKER_DOWN");
        this.workerStates.set(command.worker, false);
        return null;

      case "restart":
        require(this.workerStates.has(command.worker), "UNKNOWN_WORKER");
        require(!this.workerStates.get(command.worker), "WORKER_UP");
        this.workerStates.set(command.worker, true);
        return null;

      case "claim":
        return this.claim(command.worker);

      case "renew":
        return this.renew(command.worker, command.ticket);

      case "call":
        return this.call(command.worker, command.ticket);

      case "deliver":
        return this.deliver(command.ticket);

      case "cancel":
        this.cancel(command.run);
        return null;

      default:
        throw new DomainError("UNSUPPORTED_FEATURE");
    }
  }

  workerSnapshot(): WorkerSnapshot {
    return {
      now: this.now,
      workers: this.workflow.workers.map((w) => ({
        id: w,
        up: this.workerStates.get(w)!,
      })),
      runs: this.runs.map((run) => ({
        ...run,
        steps: run.steps.map((step) => {
          const leasingKey = this.leaseKey(run.id, step.id);
          const lease = this.leases.get(leasingKey);
          return { ...step, lease: lease ?? null };
        }),
      })),
    };
  }

  run(): WorkerSimulationResult {
    for (const command of this.workflow.commands) {
      const result = this.apply(command);
      this.results.push(result);
    }

    return {
      results: this.results,
      final: {
        ...this.workerSnapshot(),
        calls: this.service.calls,
        effects: this.service.effects,
      },
    };
  }
}
