import {
  DomainError, TIME_LIMIT, fields, identifier, integer, require,
  parseStep, validateGraph, MockService,
} from "./workflow.ts";
import type { StepDefinition, StepState, RunState } from "./workflow.ts";

type Command =
  | { op: "start" | "cancel"; run: string }
  | { op: "advance"; by: number }
  | { op: "observe" }
  | { op: "crash" | "restart" | "claim"; worker: string }
  | { op: "renew" | "call"; worker: string; ticket: number }
  | { op: "deliver"; ticket: number };
interface Workflow {
  steps: StepDefinition[];
  commands: Command[];
  workers: string[];
  maxAttempts: number;
  retryDelay: number;
  leaseDuration: number;
}
function parseCommand(raw: unknown): Command {
  require(raw !== null && typeof raw === "object" && !Array.isArray(raw));
  const obj = raw as Record<string, unknown>;
  switch (obj.op) {
    case "start": case "cancel":
      fields(raw, ["op", "run"]);
      return { op: obj.op, run: identifier(obj.run) };
    case "advance":
      fields(raw, ["op", "by"]);
      return { op: obj.op, by: integer(obj.by, 0, TIME_LIMIT) };
    case "observe":
      fields(raw, ["op"]);
      return { op: obj.op };
    case "crash": case "restart": case "claim":
      fields(raw, ["op", "worker"]);
      return { op: obj.op, worker: identifier(obj.worker) };
    case "renew": case "call":
      fields(raw, ["op", "worker", "ticket"]);
      return { op: obj.op, worker: identifier(obj.worker), ticket: integer(obj.ticket, 1, TIME_LIMIT) };
    case "deliver":
      fields(raw, ["op", "ticket"]);
      return { op: obj.op, ticket: integer(obj.ticket, 1, TIME_LIMIT) };
    default: throw new DomainError("INVALID_INPUT");
  }
}
export function parseLeasedWorkflow(raw: unknown): Workflow {
  const obj = fields(raw, ["steps", "commands", "workers"], ["max_attempts", "retry_delay", "lease_duration"]);
  require(Array.isArray(obj.steps) && obj.steps.length > 0);
  require(Array.isArray(obj.commands) && obj.commands.length <= 2000);
  require(Array.isArray(obj.workers) && obj.workers.length > 0);
  const workers = obj.workers.map(identifier);
  require(new Set(workers).size === workers.length);
  const workflow: Workflow = {
    steps: obj.steps.map(parseStep), commands: obj.commands.map(parseCommand), workers,
    maxAttempts: integer(Object.hasOwn(obj, "max_attempts") ? obj.max_attempts : 3, 1, 10),
    retryDelay: integer(Object.hasOwn(obj, "retry_delay") ? obj.retry_delay : 2, 1, 1_000_000),
    leaseDuration: integer(Object.hasOwn(obj, "lease_duration") ? obj.lease_duration : 5, 1, 1_000_000),
  };
  validateGraph(workflow.steps);
  return workflow;
}
interface Lease { worker: string; ticket: number; expires: number }
interface Step extends StepState { lease: Lease | null }
interface Run extends Omit<RunState, "status" | "steps"> {
  status: RunState["status"] | "failing";
  steps: Step[];
}
type Receipt =
  | { kind: "execute"; outcome: "applied" | "replayed" | "transient" }
  | { kind: "lookup"; outcome: "found" | "missing" };
interface Ticket {
  id: number;
  worker: string;
  run: Run;
  step: Step;
  definition: StepDefinition;
  attempt: number;
  kind: Receipt["kind"];
  receipt?: Receipt;
  delivered: boolean;
}

/** Lease acquisitions and transport receipts survive independently of workers. */
export class LeasedSimulator {
  private now = 0;
  private readonly workflow: Workflow;
  private readonly workers: { id: string; up: boolean }[];
  private readonly runs: Run[] = [];
  private readonly tickets = new Map<number, Ticket>();
  private readonly service = new MockService();
  private readonly calls: (MockService["calls"][number] & { worker: string; ticket: number })[] = [];
  constructor(workflow: Workflow) {
    this.workflow = workflow;
    this.workers = workflow.workers.map(id => ({ id, up: true }));
  }
  private deadline(delay: number): number {
    const deadline = this.now + delay;
    require(deadline <= TIME_LIMIT, "TIME_OVERFLOW");
    return deadline;
  }
  private ticket(id: number): Ticket {
    const ticket = this.tickets.get(id);
    require(ticket, "UNKNOWN_TICKET");
    return ticket;
  }
  private live(ticket: Ticket): boolean {
    const lease = ticket.step.lease;
    return ticket.step.status === "running" && lease !== null &&
      lease.ticket === ticket.id && lease.worker === ticket.worker && this.now < lease.expires;
  }
  private select(): { run: Run; step: Step; definition: StepDefinition } | undefined {
    for (const run of this.runs) {
      const index = run.steps.findIndex(step => step.status === "running" &&
        step.lease !== null && step.lease.expires <= this.now);
      if (index >= 0) return { run, step: run.steps[index]!, definition: this.workflow.steps[index]! };
    }
    for (const run of this.runs) {
      if (run.status !== "active") continue;
      const index = run.steps.findIndex((step, i) => step.status === "pending" &&
        step.ready_at <= this.now && this.workflow.steps[i]!.needs.every(dep =>
          run.steps.some(other => other.id === dep && other.status === "succeeded")));
      if (index >= 0) return { run, step: run.steps[index]!, definition: this.workflow.steps[index]! };
    }
    return undefined;
  }
  private claim(worker: string): { ticket: number | null } {
    require(!this.runs.some(run => run.steps.some(step => step.lease?.worker === worker &&
      this.now < step.lease.expires)), "WORKER_BUSY");
    const action = this.select();
    if (!action) return { ticket: null };
    const expires = this.deadline(this.workflow.leaseDuration);
    const id = this.tickets.size + 1;
    const { run, step } = action;
    if (step.status === "pending") {
      step.status = "running";
      step.attempts++;
    }
    step.lease = { worker, ticket: id, expires };
    this.tickets.set(id, {
      ...action, id, worker, attempt: step.attempts,
      kind: run.status === "active" ? "execute" : "lookup", delivered: false,
    });
    return { ticket: id };
  }
  private call(ticket: Ticket): Receipt | { outcome: "stale" } {
    if (!this.live(ticket)) return { outcome: "stale" };
    if (!ticket.receipt) {
      const key: [string, string] = [ticket.run.id, ticket.step.id];
      ticket.receipt = ticket.kind === "execute"
        ? { kind: "execute", outcome: this.service.execute(key, ticket.definition.amount,
            ticket.attempt, ticket.definition.failures) }
        : { kind: "lookup", outcome: this.service.lookup(key, ticket.attempt) };
      this.calls.push({ worker: ticket.worker, ticket: ticket.id, key,
        attempt: ticket.attempt, ...ticket.receipt });
    }
    return { ...ticket.receipt };
  }
  /** Revocation leaves the old lease visible, but fences all its messages. */
  private expireRunning(run: Run): void {
    for (const step of run.steps)
      if (step.status === "running" && step.lease) step.lease.expires = this.now;
  }
  private deliver(ticket: Ticket): { committed: boolean } {
    if (ticket.delivered || !ticket.receipt || !this.live(ticket) ||
        !this.workers.find(worker => worker.id === ticket.worker)!.up)
      return { committed: false };
    const { step, run, receipt } = ticket;
    if (receipt.kind === "lookup") {
      step.status = receipt.outcome === "found" ? "succeeded"
        : run.status === "cancelling" ? "cancelled" : "blocked";
    } else if (receipt.outcome === "transient") {
      if (step.attempts < this.workflow.maxAttempts) {
        step.ready_at = this.deadline(this.workflow.retryDelay);
        step.status = "pending";
      } else {
        step.status = "failed";
        for (const other of run.steps)
          if (other.status === "pending") other.status = "blocked";
        run.status = "failing";
        this.expireRunning(run);
      }
    } else {
      step.status = "succeeded";
    }
    step.lease = null;
    ticket.delivered = true;
    if (!run.steps.some(other => other.status === "running")) {
      if (run.status === "cancelling") run.status = "cancelled";
      else if (run.status === "failing") run.status = "failed";
      else if (run.steps.every(other => other.status === "succeeded")) run.status = "succeeded";
    }
    return { committed: true };
  }
  private apply(command: Command): unknown {
    // Availability errors precede ticket validation, even for stale tickets.
    if ("worker" in command) {
      const worker = this.workers.find(worker => worker.id === command.worker);
      require(worker, "UNKNOWN_WORKER");
      if (command.op === "restart") {
        require(!worker.up, "WORKER_UP");
        worker.up = true;
        return null;
      }
      require(worker.up, "WORKER_DOWN");
      if (command.op === "crash") { worker.up = false; return null; }
      if (command.op === "claim") return this.claim(worker.id);
      if ("ticket" in command) {
        const ticket = this.ticket(command.ticket);
        require(ticket.worker === worker.id, "WRONG_WORKER");
        if (command.op === "call") return this.call(ticket);
        if (!this.live(ticket)) return { renewed: false };
        ticket.step.lease!.expires = this.deadline(this.workflow.leaseDuration);
        return { renewed: true };
      }
    }
    switch (command.op) {
      case "start":
        require(!this.runs.some(run => run.id === command.run), "DUPLICATE_RUN");
        this.runs.push({ id: command.run, status: "active", cancel_requested: false,
          steps: this.workflow.steps.map(step => ({ id: step.id, status: "pending",
            attempts: 0, ready_at: this.now, lease: null })) });
        return null;
      case "advance": this.now = this.deadline(command.by); return null;
      case "observe": return this.snapshot();
      case "deliver": return this.deliver(this.ticket(command.ticket));
      case "cancel": {
        const run = this.runs.find(run => run.id === command.run);
        require(run, "UNKNOWN_RUN");
        if (run.status !== "active") return null;
        run.cancel_requested = true;
        for (const step of run.steps)
          if (step.status === "pending") step.status = "cancelled";
        this.expireRunning(run);
        run.status = run.steps.some(step => step.status === "running") ? "cancelling" : "cancelled";
        return null;
      }
    }
  }
  private snapshot() {
    return { now: this.now, workers: this.workers.map(worker => ({ ...worker })),
      runs: this.runs.map(run => ({ ...run, steps: run.steps.map(step => ({ ...step,
        lease: step.lease ? { ...step.lease } : null })) })) };
  }
  run() {
    const results = this.workflow.commands.map(command => this.apply(command));
    return { results, final: { ...this.snapshot(), calls: this.calls, effects: this.service.effects } };
  }
}
