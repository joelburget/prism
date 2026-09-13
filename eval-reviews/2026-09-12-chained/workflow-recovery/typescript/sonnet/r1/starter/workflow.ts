/** Deterministic in-memory DAG runner with durable recovery, retries, cancellation, and leased workers. */
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
export type StepStatus =
  | "pending"
  | "running"
  | "succeeded"
  | "failed"
  | "blocked"
  | "cancelled";
export type RunStatus =
  | "active"
  | "succeeded"
  | "failed"
  | "cancelling"
  | "cancelled";
export interface StepState {
  id: string;
  status: StepStatus;
  attempts: number;
  ready_at: number;
}
export interface RunState {
  id: string;
  status: RunStatus;
  cancel_requested: boolean;
  steps: StepState[];
}
type ActionKey = readonly [string, string];
type ExecuteOutcome = "applied" | "replayed" | "transient";
type LookupOutcome = "found" | "missing";
interface ExecuteCall {
  kind: "execute";
  key: ActionKey;
  attempt: number;
  outcome: ExecuteOutcome;
}
interface LookupCall {
  kind: "lookup";
  key: ActionKey;
  attempt: number;
  outcome: LookupOutcome;
}
type ServiceCall = ExecuteCall | LookupCall;
interface Effect {
  key: ActionKey;
  amount: number;
}
function keyString(key: ActionKey): string {
  // Steps/run IDs cannot contain a space, so this delimiter is collision-free.
  return `${key[0]} ${key[1]}`;
}
class Ledger {
  readonly effects: Effect[] = [];
  private readonly effectIndex = new Map<string, Effect>();
  private readonly transientCounts = new Map<string, number>();
  execute(key: ActionKey, amount: number, failures: number): ExecuteOutcome {
    const k = keyString(key);
    if (this.effectIndex.has(k)) return "replayed";
    const count = this.transientCounts.get(k) ?? 0;
    if (count < failures) {
      this.transientCounts.set(k, count + 1);
      return "transient";
    }
    const effect: Effect = { key, amount };
    this.effectIndex.set(k, effect);
    this.effects.push(effect);
    return "applied";
  }
  lookup(key: ActionKey): LookupOutcome {
    return this.effectIndex.has(keyString(key)) ? "found" : "missing";
  }
}
export class MockService {
  readonly calls: ServiceCall[] = [];
  private readonly ledger = new Ledger();
  get effects(): Effect[] {
    return this.ledger.effects;
  }
  execute(
    key: ActionKey,
    amount: number,
    attempt: number,
    failures: number,
  ): ExecuteOutcome {
    const outcome = this.ledger.execute(key, amount, failures);
    this.calls.push({ kind: "execute", key, attempt, outcome });
    return outcome;
  }
  lookup(key: ActionKey, attempt: number): LookupOutcome {
    const outcome = this.ledger.lookup(key);
    this.calls.push({ kind: "lookup", key, attempt, outcome });
    return outcome;
  }
}

// ---------------------------------------------------------------------------
// Checkpoint one: single-process durable runner.
// ---------------------------------------------------------------------------

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
  tick(crashAt: "after_begin" | "after_call" | null): void {
    const action = this.selectAction();
    if (!action) return;
    const { run, step, definition } = action;
    const key: ActionKey = [run.id, step.id];
    if (step.status === "pending") {
      step.status = "running";
      step.attempts += 1;
    }
    if (crashAt === "after_begin") {
      this.up = false;
      return;
    }
    if (run.status === "cancelling") {
      const outcome = this.service.lookup(key, step.attempts);
      if (crashAt === "after_call") {
        this.up = false;
        return;
      }
      step.status = outcome === "found" ? "succeeded" : "cancelled";
      run.status = "cancelled";
      return;
    }
    const outcome = this.service.execute(
      key,
      definition.amount,
      step.attempts,
      definition.failures,
    );
    if (crashAt === "after_call") {
      this.up = false;
      return;
    }
    if (outcome === "applied" || outcome === "replayed") {
      step.status = "succeeded";
      if (run.steps.every((state) => state.status === "succeeded"))
        run.status = "succeeded";
      return;
    }
    // Committed transient response: schedule a retry or exhaust the budget.
    if (step.attempts < this.workflow.maxAttempts) {
      const readyAt = this.now + this.workflow.retryDelay;
      require(readyAt <= TIME_LIMIT, "TIME_OVERFLOW");
      step.status = "pending";
      step.ready_at = readyAt;
    } else {
      step.status = "failed";
      run.status = "failed";
      for (const other of run.steps)
        if (other.status === "pending") other.status = "blocked";
    }
  }
  apply(command: Command): void {
    switch (command.op) {
      case "start":
        require(this.up, "PROCESS_DOWN");
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
      case "cancel": {
        require(this.up, "PROCESS_DOWN");
        const run = this.runs.find((candidate) => candidate.id === command.run);
        require(run !== undefined, "UNKNOWN_RUN");
        if (
          run.status === "succeeded" ||
          run.status === "failed" ||
          run.status === "cancelled"
        )
          break;
        run.cancel_requested = true;
        for (const step of run.steps)
          if (step.status === "pending") step.status = "cancelled";
        run.status = run.steps.some((step) => step.status === "running")
          ? "cancelling"
          : "cancelled";
        break;
      }
      case "tick":
        require(this.up, "PROCESS_DOWN");
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
        require(this.up, "PROCESS_DOWN");
        this.up = false;
        break;
      case "restart":
        require(!this.up, "PROCESS_UP");
        this.up = true;
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

// ---------------------------------------------------------------------------
// Checkpoint two: leased workers, fenced recovery.
// ---------------------------------------------------------------------------

export type LeasedRunStatus = RunStatus | "failing";
export interface Lease {
  worker: string;
  ticket: number;
  expires: number;
}
export interface LeasedStepState {
  id: string;
  status: StepStatus;
  attempts: number;
  ready_at: number;
  lease: Lease | null;
}
export interface LeasedRunState {
  id: string;
  status: LeasedRunStatus;
  cancel_requested: boolean;
  steps: LeasedStepState[];
}
export type LeasedCommand =
  | { op: "start" | "cancel"; run: string }
  | { op: "advance"; by: number }
  | { op: "observe" }
  | { op: "crash" | "restart" | "claim"; worker: string }
  | { op: "renew" | "call"; worker: string; ticket: number }
  | { op: "deliver"; ticket: number };
export interface LeasedWorkflow {
  readonly steps: readonly StepDefinition[];
  readonly commands: readonly LeasedCommand[];
  readonly workers: readonly string[];
  readonly maxAttempts: number;
  readonly retryDelay: number;
  readonly leaseDuration: number;
}
function parseLeasedCommand(raw: unknown): LeasedCommand {
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
    case "claim":
      fields(raw, ["op", "worker"]);
      return { op: obj.op, worker: identifier(obj.worker) };
    case "renew":
    case "call":
      fields(raw, ["op", "worker", "ticket"]);
      return {
        op: obj.op,
        worker: identifier(obj.worker),
        ticket: integer(obj.ticket, 1, 2_147_483_647),
      };
    case "deliver":
      fields(raw, ["op", "ticket"]);
      return { op: obj.op, ticket: integer(obj.ticket, 1, 2_147_483_647) };
    default:
      throw new DomainError("INVALID_INPUT");
  }
}
export function parseLeasedWorkflow(raw: unknown): LeasedWorkflow {
  const obj = fields(
    raw,
    ["steps", "commands", "workers"],
    ["max_attempts", "retry_delay", "lease_duration"],
  );
  require(
    Array.isArray(obj.steps) &&
      obj.steps.length > 0 &&
      Array.isArray(obj.commands) &&
      Array.isArray(obj.workers),
  );
  require(obj.commands.length <= 2000);
  const workers = obj.workers.map(identifier);
  require(workers.length > 0);
  require(new Set(workers).size === workers.length);
  const workflow: LeasedWorkflow = {
    steps: obj.steps.map(parseStep),
    commands: obj.commands.map(parseLeasedCommand),
    workers,
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
  return workflow;
}
interface LeasedCall {
  worker: string;
  ticket: number;
  kind: "execute" | "lookup";
  key: ActionKey;
  attempt: number;
  outcome: ExecuteOutcome | LookupOutcome;
}
class LeasedService {
  readonly calls: LeasedCall[] = [];
  private readonly ledger = new Ledger();
  get effects(): Effect[] {
    return this.ledger.effects;
  }
  execute(
    worker: string,
    ticket: number,
    key: ActionKey,
    amount: number,
    attempt: number,
    failures: number,
  ): ExecuteOutcome {
    const outcome = this.ledger.execute(key, amount, failures);
    this.calls.push({ worker, ticket, kind: "execute", key, attempt, outcome });
    return outcome;
  }
  lookup(
    worker: string,
    ticket: number,
    key: ActionKey,
    attempt: number,
  ): LookupOutcome {
    const outcome = this.ledger.lookup(key);
    this.calls.push({ worker, ticket, kind: "lookup", key, attempt, outcome });
    return outcome;
  }
}
type TicketResponse =
  | { kind: "execute"; outcome: ExecuteOutcome }
  | { kind: "lookup"; outcome: LookupOutcome };
interface TicketRecord {
  id: number;
  worker: string;
  runId: string;
  stepId: string;
  kind: "execute" | "lookup";
  attempt: number;
  response: TicketResponse | null;
  delivered: boolean;
}
export interface LeasedSnapshot {
  now: number;
  workers: { id: string; up: boolean }[];
  runs: LeasedRunState[];
}
export interface LeasedSimulationResult {
  results: unknown[];
  final: LeasedSnapshot & { calls: LeasedCall[]; effects: Effect[] };
}
export class LeasedSimulator {
  readonly workflow: LeasedWorkflow;
  now = 0;
  private readonly workers = new Map<string, { up: boolean }>();
  readonly runs: LeasedRunState[] = [];
  private readonly service = new LeasedService();
  private readonly tickets = new Map<number, TicketRecord>();
  private nextTicket = 1;
  constructor(workflow: LeasedWorkflow) {
    this.workflow = workflow;
    for (const id of workflow.workers) this.workers.set(id, { up: true });
  }
  private findRun(id: string): LeasedRunState | undefined {
    return this.runs.find((run) => run.id === id);
  }
  private getWorker(id: string): { up: boolean } {
    const worker = this.workers.get(id);
    require(worker !== undefined, "UNKNOWN_WORKER");
    return worker!;
  }
  private getTicket(id: number): TicketRecord {
    const ticket = this.tickets.get(id);
    require(ticket !== undefined, "UNKNOWN_TICKET");
    return ticket!;
  }
  private stepDefOf(stepId: string): StepDefinition {
    return this.workflow.steps.find((step) => step.id === stepId)!;
  }
  private anyLiveLeaseFor(worker: string): boolean {
    for (const run of this.runs)
      for (const step of run.steps)
        if (
          step.lease &&
          step.lease.worker === worker &&
          this.now < step.lease.expires
        )
          return true;
    return false;
  }
  private selectWork():
    | { run: LeasedRunState; step: LeasedStepState; fresh: boolean }
    | undefined {
    for (const run of this.runs) {
      for (const step of run.steps) {
        if (
          step.status === "running" &&
          step.lease &&
          this.now >= step.lease.expires
        )
          return { run, step, fresh: false };
      }
    }
    for (const run of this.runs) {
      if (run.status !== "active") continue;
      const succeeded = new Set(
        run.steps.filter((step) => step.status === "succeeded").map((step) => step.id),
      );
      for (const [index, step] of run.steps.entries()) {
        const definition = this.workflow.steps[index]!;
        if (
          step.status === "pending" &&
          step.ready_at <= this.now &&
          definition.needs.every((dep) => succeeded.has(dep))
        )
          return { run, step, fresh: true };
      }
    }
    return undefined;
  }
  private beginFailureDrain(run: LeasedRunState): void {
    for (const step of run.steps)
      if (step.status === "pending") step.status = "blocked";
    const stillRunning = run.steps.some((step) => step.status === "running");
    if (stillRunning) {
      run.status = "failing";
      for (const step of run.steps)
        if (step.status === "running" && step.lease)
          step.lease.expires = this.now;
    } else {
      run.status = "failed";
    }
  }
  private reconcileDraining(run: LeasedRunState): void {
    const stillRunning = run.steps.some((step) => step.status === "running");
    if (!stillRunning)
      run.status = run.status === "cancelling" ? "cancelled" : "failed";
  }
  private commitResponse(
    run: LeasedRunState,
    step: LeasedStepState,
    ticket: TicketRecord,
  ): void {
    const response = ticket.response!;
    if (response.kind === "execute") {
      if (response.outcome === "applied" || response.outcome === "replayed") {
        step.status = "succeeded";
        if (
          run.status === "active" &&
          run.steps.every((state) => state.status === "succeeded")
        )
          run.status = "succeeded";
      } else if (step.attempts < this.workflow.maxAttempts) {
        const readyAt = this.now + this.workflow.retryDelay;
        require(readyAt <= TIME_LIMIT, "TIME_OVERFLOW");
        step.status = "pending";
        step.ready_at = readyAt;
      } else {
        step.status = "failed";
        this.beginFailureDrain(run);
      }
    } else {
      step.status =
        response.outcome === "found"
          ? "succeeded"
          : run.status === "cancelling"
            ? "cancelled"
            : "blocked";
      this.reconcileDraining(run);
    }
  }
  apply(command: LeasedCommand): unknown {
    switch (command.op) {
      case "start": {
        require(this.runs.every((run) => run.id !== command.run), "DUPLICATE_RUN");
        this.runs.push({
          id: command.run,
          status: "active",
          cancel_requested: false,
          steps: this.workflow.steps.map((step) => ({
            id: step.id,
            status: "pending",
            attempts: 0,
            ready_at: this.now,
            lease: null,
          })),
        });
        return null;
      }
      case "advance": {
        require(this.now + command.by <= TIME_LIMIT, "TIME_OVERFLOW");
        this.now += command.by;
        return null;
      }
      case "observe":
        return this.snapshot();
      case "crash": {
        const worker = this.getWorker(command.worker);
        require(worker.up, "WORKER_DOWN");
        worker.up = false;
        return null;
      }
      case "restart": {
        const worker = this.getWorker(command.worker);
        require(!worker.up, "WORKER_UP");
        worker.up = true;
        return null;
      }
      case "claim": {
        const worker = this.getWorker(command.worker);
        require(worker.up, "WORKER_DOWN");
        require(!this.anyLiveLeaseFor(command.worker), "WORKER_BUSY");
        const found = this.selectWork();
        if (!found) return { ticket: null };
        const { run, step, fresh } = found;
        if (fresh) {
          step.status = "running";
          step.attempts += 1;
        }
        const expires = this.now + this.workflow.leaseDuration;
        require(expires <= TIME_LIMIT, "TIME_OVERFLOW");
        const ticketId = this.nextTicket++;
        step.lease = { worker: command.worker, ticket: ticketId, expires };
        const kind: "execute" | "lookup" =
          run.status === "active" ? "execute" : "lookup";
        this.tickets.set(ticketId, {
          id: ticketId,
          worker: command.worker,
          runId: run.id,
          stepId: step.id,
          kind,
          attempt: step.attempts,
          response: null,
          delivered: false,
        });
        return { ticket: ticketId };
      }
      case "renew": {
        const worker = this.getWorker(command.worker);
        require(worker.up, "WORKER_DOWN");
        const ticket = this.getTicket(command.ticket);
        require(ticket.worker === command.worker, "WRONG_WORKER");
        const run = this.findRun(ticket.runId)!;
        const step = run.steps.find((state) => state.id === ticket.stepId)!;
        if (
          step.lease &&
          step.lease.ticket === ticket.id &&
          this.now < step.lease.expires
        ) {
          const expires = this.now + this.workflow.leaseDuration;
          require(expires <= TIME_LIMIT, "TIME_OVERFLOW");
          step.lease.expires = expires;
          return { renewed: true };
        }
        return { renewed: false };
      }
      case "call": {
        const worker = this.getWorker(command.worker);
        require(worker.up, "WORKER_DOWN");
        const ticket = this.getTicket(command.ticket);
        require(ticket.worker === command.worker, "WRONG_WORKER");
        const run = this.findRun(ticket.runId)!;
        const step = run.steps.find((state) => state.id === ticket.stepId)!;
        const current =
          step.lease !== null &&
          step.lease.ticket === ticket.id &&
          this.now < step.lease.expires;
        if (!current) return { outcome: "stale" };
        if (ticket.response === null) {
          const key: ActionKey = [run.id, step.id];
          if (ticket.kind === "execute") {
            const definition = this.stepDefOf(step.id);
            const outcome = this.service.execute(
              ticket.worker,
              ticket.id,
              key,
              definition.amount,
              ticket.attempt,
              definition.failures,
            );
            ticket.response = { kind: "execute", outcome };
          } else {
            const outcome = this.service.lookup(
              ticket.worker,
              ticket.id,
              key,
              ticket.attempt,
            );
            ticket.response = { kind: "lookup", outcome };
          }
        }
        return { ...ticket.response };
      }
      case "deliver": {
        const ticket = this.getTicket(command.ticket);
        const run = this.findRun(ticket.runId)!;
        const step = run.steps.find((state) => state.id === ticket.stepId)!;
        const current = step.lease !== null && step.lease.ticket === ticket.id;
        const live = current && this.now < step.lease!.expires;
        const ownerUp = this.workers.get(ticket.worker)!.up;
        if (ticket.response === null || ticket.delivered || !current || !live || !ownerUp)
          return { committed: false };
        ticket.delivered = true;
        step.lease = null;
        this.commitResponse(run, step, ticket);
        return { committed: true };
      }
      case "cancel": {
        const run = this.findRun(command.run);
        require(run !== undefined, "UNKNOWN_RUN");
        if (
          run.status === "succeeded" ||
          run.status === "failed" ||
          run.status === "cancelled" ||
          run.status === "failing"
        )
          return null;
        if (run.cancel_requested) return null;
        run.cancel_requested = true;
        for (const step of run.steps)
          if (step.status === "pending") step.status = "cancelled";
        for (const step of run.steps)
          if (step.status === "running" && step.lease)
            step.lease.expires = this.now;
        run.status = run.steps.some((step) => step.status === "running")
          ? "cancelling"
          : "cancelled";
        return null;
      }
    }
  }
  snapshot(): LeasedSnapshot {
    return {
      now: this.now,
      workers: this.workflow.workers.map((id) => ({
        id,
        up: this.workers.get(id)!.up,
      })),
      runs: this.runs.map((run) => ({
        id: run.id,
        status: run.status,
        cancel_requested: run.cancel_requested,
        steps: run.steps.map((step) => ({
          id: step.id,
          status: step.status,
          attempts: step.attempts,
          ready_at: step.ready_at,
          lease: step.lease ? { ...step.lease } : null,
        })),
      })),
    };
  }
  run(): LeasedSimulationResult {
    const results = this.workflow.commands.map((command) => this.apply(command));
    return {
      results,
      final: {
        ...this.snapshot(),
        calls: this.service.calls,
        effects: this.service.effects,
      },
    };
  }
}
