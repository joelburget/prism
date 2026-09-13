/**
 * Checkpoint two: leased workers with fenced recovery. Selected when the
 * request carries a `workers` field; the checkpoint-one simulator in
 * workflow.ts is otherwise used unchanged.
 */
import {
  DomainError,
  MockService,
  TIME_LIMIT,
  fields,
  identifier,
  integer,
  parseStep,
  require,
  validateGraph,
  type ActionKey,
  type Effect,
  type ExecuteOutcome,
  type LookupOutcome,
  type ServiceCall,
  type StepDefinition,
} from "./workflow.ts";

export const MAX_COMMANDS = 2_000;
export const MAX_WORKERS = 100;
export const MAX_STEPS = 200;

export type LeasedCommand =
  | { op: "start" | "cancel"; run: string }
  | { op: "advance"; by: number }
  | { op: "observe" }
  | { op: "crash" | "restart" | "claim"; worker: string }
  | { op: "renew" | "call"; worker: string; ticket: number }
  | { op: "deliver"; ticket: number };

export interface LeasedWorkflow {
  readonly steps: readonly StepDefinition[];
  readonly workers: readonly string[];
  readonly commands: readonly LeasedCommand[];
  readonly maxAttempts: number;
  readonly retryDelay: number;
  readonly leaseDuration: number;
}

function parseCommand(raw: unknown): LeasedCommand {
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
        ticket: integer(obj.ticket, 1, TIME_LIMIT),
      };
    case "deliver":
      fields(raw, ["op", "ticket"]);
      return { op: obj.op, ticket: integer(obj.ticket, 1, TIME_LIMIT) };
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
      obj.steps.length <= MAX_STEPS &&
      Array.isArray(obj.commands) &&
      obj.commands.length <= MAX_COMMANDS &&
      Array.isArray(obj.workers) &&
      obj.workers.length > 0 &&
      obj.workers.length <= MAX_WORKERS,
  );
  const workers = obj.workers.map(identifier);
  require(new Set(workers).size === workers.length);
  const optional = (key: string, fallback: number, low: number, high: number) =>
    integer(Object.hasOwn(obj, key) ? obj[key] : fallback, low, high);
  const workflow: LeasedWorkflow = {
    steps: obj.steps.map(parseStep),
    workers,
    commands: obj.commands.map(parseCommand),
    maxAttempts: optional("max_attempts", 3, 1, 10),
    retryDelay: optional("retry_delay", 2, 1, 1_000_000),
    leaseDuration: optional("lease_duration", 5, 1, 1_000_000),
  };
  validateGraph(workflow.steps); // Shape/scalar validation precedes graph checks.
  return workflow;
}

export type LeasedStepStatus =
  | "pending"
  | "running"
  | "succeeded"
  | "failed"
  | "blocked"
  | "cancelled";
export type LeasedRunStatus =
  | "active"
  | "succeeded"
  | "failed"
  | "cancelling"
  | "cancelled"
  | "failing";
export interface Lease {
  worker: string;
  ticket: number;
  expires: number;
}
export interface LeasedStepState {
  id: string;
  status: LeasedStepStatus;
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
export interface WorkerState {
  id: string;
  up: boolean;
}
export type Receipt =
  | { kind: "execute"; outcome: ExecuteOutcome }
  | { kind: "lookup"; outcome: LookupOutcome };
/** Durable metadata for one lease acquisition, retained after replacement. */
interface Ticket {
  readonly ticket: number;
  readonly worker: string;
  readonly runIndex: number;
  readonly stepIndex: number;
  readonly kind: "execute" | "lookup";
  readonly attempt: number;
  response: Receipt | null; // Delayed transport message; survives crashes.
  delivered: boolean;
}
export interface LeasedSnapshot {
  now: number;
  workers: WorkerState[];
  runs: LeasedRunState[];
}
export type CommandValue =
  | null
  | LeasedSnapshot
  | { ticket: number | null }
  | { renewed: boolean }
  | { committed: boolean }
  | Receipt
  | { outcome: "stale" };
export interface LeasedResult {
  results: CommandValue[];
  final: LeasedSnapshot & { calls: ServiceCall[]; effects: Effect[] };
}

const NO_CANCEL: ReadonlySet<LeasedRunStatus> = new Set([
  "succeeded",
  "failed",
  "cancelled",
  "failing",
]);

export class LeasedSimulator {
  readonly workflow: LeasedWorkflow;
  now = 0;
  readonly workers: WorkerState[];
  readonly runs: LeasedRunState[] = [];
  readonly service: MockService;
  private readonly tickets = new Map<number, Ticket>();
  private nextTicket = 1;
  constructor(workflow: LeasedWorkflow) {
    this.workflow = workflow;
    this.workers = workflow.workers.map((id) => ({ id, up: true }));
    this.service = new MockService(workflow.steps);
  }
  private live(lease: Lease | null): lease is Lease {
    return lease !== null && this.now < lease.expires;
  }
  private worker(id: string): WorkerState {
    const worker = this.workers.find((candidate) => candidate.id === id);
    require(worker !== undefined, "UNKNOWN_WORKER");
    return worker;
  }
  private upWorker(id: string): WorkerState {
    const worker = this.worker(id);
    require(worker.up, "WORKER_DOWN");
    return worker;
  }
  private ownedTicket(worker: string, id: number): Ticket {
    const ticket = this.tickets.get(id);
    require(ticket !== undefined, "UNKNOWN_TICKET");
    require(ticket.worker === worker, "WRONG_WORKER");
    return ticket;
  }
  private stepOf(ticket: Ticket): LeasedStepState {
    return this.runs[ticket.runIndex]!.steps[ticket.stepIndex]!;
  }
  /** True when the ticket is the step's current lease and that lease is live. */
  private current(ticket: Ticket): boolean {
    const lease = this.stepOf(ticket).lease;
    return this.live(lease) && lease.ticket === ticket.ticket;
  }
  private deadline(base: number, delta: number): number {
    require(base + delta <= TIME_LIMIT, "TIME_OVERFLOW");
    return base + delta;
  }
  /** Revoke every running lease of a run so earlier responses become stale. */
  private expireRunning(run: LeasedRunState): void {
    for (const step of run.steps)
      if (step.status === "running" && step.lease) step.lease.expires = this.now;
  }
  private selectWork(): { runIndex: number; stepIndex: number } | undefined {
    for (const [runIndex, run] of this.runs.entries())
      for (const [stepIndex, step] of run.steps.entries())
        if (step.status === "running" && !this.live(step.lease))
          return { runIndex, stepIndex };
    for (const [runIndex, run] of this.runs.entries()) {
      if (run.status !== "active") continue;
      const succeeded = new Set(
        run.steps
          .filter((step) => step.status === "succeeded")
          .map((step) => step.id),
      );
      for (const [stepIndex, step] of run.steps.entries()) {
        const definition = this.workflow.steps[stepIndex]!;
        if (
          step.status === "pending" &&
          step.ready_at <= this.now &&
          definition.needs.every((dep) => succeeded.has(dep))
        )
          return { runIndex, stepIndex };
      }
    }
    return undefined;
  }
  claim(workerId: string): { ticket: number | null } {
    const worker = this.upWorker(workerId);
    require(
      !this.runs.some((run) =>
        run.steps.some(
          (step) => this.live(step.lease) && step.lease.worker === worker.id,
        ),
      ),
      "WORKER_BUSY",
    );
    const work = this.selectWork();
    if (!work) return { ticket: null };
    const run = this.runs[work.runIndex]!;
    const step = run.steps[work.stepIndex]!;
    const expires = this.deadline(this.now, this.workflow.leaseDuration);
    if (step.status === "pending") {
      step.status = "running";
      step.attempts += 1;
    }
    const ticket: Ticket = {
      ticket: this.nextTicket++,
      worker: worker.id,
      runIndex: work.runIndex,
      stepIndex: work.stepIndex,
      kind: run.status === "active" ? "execute" : "lookup",
      attempt: step.attempts,
      response: null,
      delivered: false,
    };
    this.tickets.set(ticket.ticket, ticket);
    step.lease = { worker: worker.id, ticket: ticket.ticket, expires };
    return { ticket: ticket.ticket };
  }
  renew(workerId: string, id: number): { renewed: boolean } {
    const worker = this.upWorker(workerId);
    const ticket = this.ownedTicket(worker.id, id);
    if (!this.current(ticket)) return { renewed: false };
    this.stepOf(ticket).lease!.expires = this.deadline(
      this.now,
      this.workflow.leaseDuration,
    );
    return { renewed: true };
  }
  call(workerId: string, id: number): Receipt | { outcome: "stale" } {
    const worker = this.upWorker(workerId);
    const ticket = this.ownedTicket(worker.id, id);
    if (!this.current(ticket)) return { outcome: "stale" };
    if (ticket.response) return { ...ticket.response }; // Saved receipt, no audit.
    const run = this.runs[ticket.runIndex]!;
    const step = this.stepOf(ticket);
    const key: ActionKey = [run.id, step.id];
    const origin = { worker: ticket.worker, ticket: ticket.ticket };
    ticket.response =
      ticket.kind === "execute"
        ? {
            kind: "execute",
            outcome: this.service.execute(
              key,
              this.workflow.steps[ticket.stepIndex]!.amount,
              ticket.attempt,
              origin,
            ),
          }
        : {
            kind: "lookup",
            outcome: this.service.lookup(key, ticket.attempt, origin),
          };
    return { ...ticket.response };
  }
  deliver(id: number): { committed: boolean } {
    const ticket = this.tickets.get(id);
    require(ticket !== undefined, "UNKNOWN_TICKET");
    if (
      !ticket.response ||
      ticket.delivered ||
      !this.current(ticket) ||
      !this.worker(ticket.worker).up
    )
      return { committed: false };
    const run = this.runs[ticket.runIndex]!;
    const step = this.stepOf(ticket);
    const response = ticket.response;
    if (response.kind === "execute" && response.outcome === "transient") {
      if (step.attempts < this.workflow.maxAttempts) {
        const readyAt = this.deadline(this.now, this.workflow.retryDelay);
        ticket.delivered = true;
        step.lease = null;
        step.status = "pending";
        step.ready_at = readyAt;
        return { committed: true };
      }
      ticket.delivered = true;
      step.lease = null;
      step.status = "failed";
      for (const other of run.steps)
        if (other.status === "pending") other.status = "blocked";
      if (run.steps.some((other) => other.status === "running")) {
        run.status = "failing";
        this.expireRunning(run);
      } else run.status = "failed";
      return { committed: true };
    }
    ticket.delivered = true;
    step.lease = null;
    if (response.kind === "execute") {
      step.status = "succeeded";
      if (
        run.status === "active" &&
        run.steps.every((other) => other.status === "succeeded")
      )
        run.status = "succeeded";
      return { committed: true };
    }
    const draining = run.status === "failing";
    step.status =
      response.outcome === "found"
        ? "succeeded"
        : draining
          ? "blocked"
          : "cancelled";
    if (!run.steps.some((other) => other.status === "running"))
      run.status = draining ? "failed" : "cancelled";
    return { committed: true };
  }
  cancel(run: LeasedRunState): void {
    if (NO_CANCEL.has(run.status) || run.status === "cancelling") return;
    run.cancel_requested = true;
    for (const step of run.steps)
      if (step.status === "pending") step.status = "cancelled";
    if (run.steps.some((step) => step.status === "running")) {
      run.status = "cancelling";
      this.expireRunning(run); // First cancellation only; lookups keep leases.
    } else run.status = "cancelled";
  }
  apply(command: LeasedCommand): CommandValue {
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
            status: "pending",
            attempts: 0,
            ready_at: this.now,
            lease: null,
          })),
        });
        return null;
      case "advance":
        this.now = this.deadline(this.now, command.by);
        return null;
      case "observe":
        return this.snapshot();
      case "crash":
        this.upWorker(command.worker).up = false;
        return null;
      case "restart": {
        const worker = this.worker(command.worker);
        require(!worker.up, "WORKER_UP");
        worker.up = true; // Passive: recovery happens through later claims.
        return null;
      }
      case "claim":
        return this.claim(command.worker);
      case "renew":
        return this.renew(command.worker, command.ticket);
      case "call":
        return this.call(command.worker, command.ticket);
      case "deliver":
        return this.deliver(command.ticket);
      case "cancel": {
        const run = this.runs.find((candidate) => candidate.id === command.run);
        require(run !== undefined, "UNKNOWN_RUN");
        this.cancel(run);
        return null;
      }
    }
  }
  snapshot(): LeasedSnapshot {
    return {
      now: this.now,
      workers: this.workers.map((worker) => ({ ...worker })),
      runs: this.runs.map((run) => ({
        ...run,
        steps: run.steps.map((step) => ({
          ...step,
          lease: step.lease ? { ...step.lease } : null,
        })),
      })),
    };
  }
  run(): LeasedResult {
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
