/** Deterministic in-memory DAG runner with durable recovery and idempotent effects. */
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
  status:
    | "pending"
    | "running"
    | "succeeded"
    | "failed"
    | "blocked"
    | "cancelled";
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
type LookupOutcome = "found" | "missing";
interface ServiceCall {
  kind: "execute" | "lookup";
  key: ActionKey;
  attempt: number;
  outcome: ExecuteOutcome | LookupOutcome;
}
interface Effect {
  key: ActionKey;
  amount: number;
}
/** Per-key ledger held by the mock service; it survives runner crashes. */
interface KeyRecord {
  applied: boolean;
  transients: number;
}
/** The durable, crash-proof ledger behind the mock service. It decides each
 *  atomic outcome; auditing belongs to the calling service facade. */
class EffectLedger {
  // Structured two-level lookup: run and step components never concatenate.
  private readonly ledger = new Map<string, Map<string, KeyRecord>>();
  private record(key: ActionKey): KeyRecord {
    let steps = this.ledger.get(key[0]);
    if (!steps) this.ledger.set(key[0], (steps = new Map()));
    let entry = steps.get(key[1]);
    if (!entry) steps.set(key[1], (entry = { applied: false, transients: 0 }));
    return entry;
  }
  /** Atomic: replay, fail transiently, or record exactly one effect. */
  execute(
    key: ActionKey,
    amount: number,
    failures: number,
    effects: Effect[],
  ): ExecuteOutcome {
    const entry = this.record(key);
    const outcome: ExecuteOutcome = entry.applied
      ? "replayed"
      : entry.transients < failures
        ? "transient"
        : "applied";
    if (outcome === "transient") entry.transients += 1;
    else if (outcome === "applied") {
      entry.applied = true;
      effects.push({ key, amount });
    }
    return outcome;
  }
  /** Atomic read-only probe: repeatable, never an effect or a failure. */
  lookup(key: ActionKey): LookupOutcome {
    return this.record(key).applied ? "found" : "missing";
  }
}
export class MockService {
  readonly calls: ServiceCall[] = [];
  readonly effects: Effect[] = [];
  private readonly ledger = new EffectLedger();
  /** Atomic: audit, then replay, fail transiently, or record exactly one effect. */
  execute(
    key: ActionKey,
    amount: number,
    attempt: number,
    failures: number,
  ): ExecuteOutcome {
    const outcome = this.ledger.execute(key, amount, failures, this.effects);
    this.calls.push({ kind: "execute", key, attempt, outcome });
    return outcome;
  }
  /** Atomic read-only probe: audited, repeatable, never an effect or a failure. */
  lookup(key: ActionKey, attempt: number): LookupOutcome {
    const outcome = this.ledger.lookup(key);
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
  tick(crashAt: "after_begin" | "after_call" | null): void {
    const action = this.selectAction();
    if (!action) return; // No eligible work means no checkpoint is ever reached.
    const { run, step, definition } = action;
    // Beginning an attempt is durable and happens exactly once before the call.
    if (step.status === "pending") {
      step.status = "running";
      step.attempts += 1;
    }
    if (crashAt === "after_begin") {
      this.up = false;
      return;
    }
    const key: ActionKey = [run.id, step.id];
    const reconciling = run.status === "cancelling";
    const outcome = reconciling
      ? this.service.lookup(key, step.attempts)
      : this.service.execute(
          key,
          definition.amount,
          step.attempts,
          definition.failures,
        );
    if (crashAt === "after_call") {
      this.up = false; // The response is lost before the runner commits it.
      return;
    }
    if (reconciling) {
      step.status = outcome === "found" ? "succeeded" : "cancelled";
      run.status = "cancelled";
      return;
    }
    if (outcome !== "transient") {
      step.status = "succeeded";
      if (run.steps.every((state) => state.status === "succeeded"))
        run.status = "succeeded";
      return;
    }
    if (step.attempts < this.workflow.maxAttempts) {
      require(
        this.now + this.workflow.retryDelay <= TIME_LIMIT,
        "TIME_OVERFLOW",
      );
      step.status = "pending";
      step.ready_at = this.now + this.workflow.retryDelay;
      return;
    }
    step.status = "failed";
    run.status = "failed";
    for (const other of run.steps)
      if (other.status === "pending") other.status = "blocked";
  }
  cancel(id: string): void {
    const run = this.runs.find((candidate) => candidate.id === id);
    require(run !== undefined, "UNKNOWN_RUN");
    if (run.status !== "active" && run.status !== "cancelling") return;
    run.cancel_requested = true;
    for (const step of run.steps)
      if (step.status === "pending") step.status = "cancelled";
    // A running attempt stays uncertain until a tick reconciles it by lookup.
    run.status = run.steps.some((step) => step.status === "running")
      ? "cancelling"
      : "cancelled";
  }
  apply(command: Command): void {
    // Observe and advance work in either state; everything else needs the
    // expected availability, checked before any run lookup.
    if (command.op !== "observe" && command.op !== "advance")
      require(
        this.up !== (command.op === "restart"),
        this.up ? "PROCESS_UP" : "PROCESS_DOWN",
      );
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
      case "cancel":
        this.cancel(command.run);
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
        this.up = false; // Volatile work is discarded; the durable store persists.
        break;
      case "restart":
        this.up = true; // Passive: recovery happens on the next tick.
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

/* ------------------------------------------------------------------ *
 * Checkpoint two: leased workers, delayed responses and fenced commits.
 * ------------------------------------------------------------------ */

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
const COMMAND_LIMIT = 2_000;
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
        ticket: integer(obj.ticket, 1, TIME_LIMIT),
      };
    case "deliver":
      fields(raw, ["op", "ticket"]);
      return { op: obj.op, ticket: integer(obj.ticket, 1, TIME_LIMIT) };
    default:
      // `tick` and its crash checkpoints are not part of the leased vocabulary.
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
      obj.commands.length <= COMMAND_LIMIT &&
      Array.isArray(obj.workers) &&
      obj.workers.length > 0,
  );
  const workers = obj.workers.map(identifier);
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
  validateGraph(workflow.steps); // Shape and scalar checks finish first, as before.
  return workflow;
}
export interface Lease {
  worker: string;
  ticket: number;
  expires: number;
}
export interface LeasedStepState extends StepState {
  lease: Lease | null;
}
export interface LeasedRunState {
  id: string;
  status:
    | "active"
    | "succeeded"
    | "failed"
    | "failing"
    | "cancelling"
    | "cancelled";
  cancel_requested: boolean;
  steps: LeasedStepState[];
}
interface LeasedServiceCall extends ServiceCall {
  worker: string;
  ticket: number;
}
interface WorkerState {
  id: string;
  up: boolean;
}
/** A lease acquisition: it identifies a transport message, not a logical action. */
interface TicketRecord {
  id: number;
  worker: string;
  kind: "execute" | "lookup";
  run: LeasedRunState;
  step: LeasedStepState;
  definition: StepDefinition;
  attempt: number;
  response: ExecuteOutcome | LookupOutcome | null;
  delivered: boolean;
}
/** The mock service of checkpoint one, audited per worker and acquisition. */
export class LeasedService {
  readonly calls: LeasedServiceCall[] = [];
  readonly effects: Effect[] = [];
  private readonly ledger = new EffectLedger();
  execute(
    worker: string,
    ticket: number,
    key: ActionKey,
    amount: number,
    attempt: number,
    failures: number,
  ): ExecuteOutcome {
    const outcome = this.ledger.execute(key, amount, failures, this.effects);
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
export interface LeasedSnapshot {
  now: number;
  workers: WorkerState[];
  runs: LeasedRunState[];
}
export interface LeasedResult {
  results: unknown[];
  final: LeasedSnapshot & {
    calls: LeasedServiceCall[];
    effects: Effect[];
  };
}
interface LeasedAction {
  run: LeasedRunState;
  step: LeasedStepState;
  definition: StepDefinition;
}
export class LeasedSimulator {
  readonly workflow: LeasedWorkflow;
  now = 0;
  readonly workers: WorkerState[];
  readonly runs: LeasedRunState[] = [];
  readonly service = new LeasedService();
  readonly results: unknown[] = [];
  private nextTicket = 1;
  private readonly tickets = new Map<number, TicketRecord>();
  constructor(workflow: LeasedWorkflow) {
    this.workflow = workflow;
    this.workers = workflow.workers.map((id) => ({ id, up: true }));
  }
  private worker(id: string): WorkerState {
    const found = this.workers.find((candidate) => candidate.id === id);
    require(found !== undefined, "UNKNOWN_WORKER");
    return found;
  }
  /** Existence, then availability, then ticket existence and ownership. */
  private available(id: string): WorkerState {
    const worker = this.worker(id);
    require(worker.up, "WORKER_DOWN");
    return worker;
  }
  private ticket(id: number, owner?: string): TicketRecord {
    const found = this.tickets.get(id);
    require(found !== undefined, "UNKNOWN_TICKET");
    // Ownership stays with the original worker even after a reclaim.
    require(owner === undefined || found.worker === owner, "WRONG_WORKER");
    return found;
  }
  private live(lease: Lease | null): boolean {
    return lease !== null && this.now < lease.expires; // Equality is expired.
  }
  private current(ticket: TicketRecord): boolean {
    return ticket.step.lease !== null && ticket.step.lease.ticket === ticket.id;
  }
  private deadline(): number {
    require(
      this.now + this.workflow.leaseDuration <= TIME_LIMIT,
      "TIME_OVERFLOW",
    );
    return this.now + this.workflow.leaseDuration;
  }
  /** Recovery of expired running work first, then ready pending work. */
  private selectAction(): LeasedAction | undefined {
    for (const run of this.runs)
      for (const [index, step] of run.steps.entries())
        if (step.status === "running" && !this.live(step.lease))
          return { run, step, definition: this.workflow.steps[index]! };
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
  claim(id: string): { ticket: number | null } {
    const worker = this.available(id);
    require(
      !this.runs.some((run) =>
        run.steps.some(
          (step) => step.lease?.worker === worker.id && this.live(step.lease),
        ),
      ),
      "WORKER_BUSY",
    );
    const action = this.selectAction();
    if (!action) return { ticket: null }; // No eligible work consumes no ID.
    const { run, step, definition } = action;
    const expires = this.deadline();
    // Starting pending work begins a new attempt; recovery reuses the old one.
    if (step.status === "pending") {
      step.status = "running";
      step.attempts += 1;
    }
    const ticket = this.nextTicket++;
    step.lease = { worker: worker.id, ticket, expires };
    this.tickets.set(ticket, {
      id: ticket,
      worker: worker.id,
      kind: run.status === "active" ? "execute" : "lookup",
      run,
      step,
      definition,
      attempt: step.attempts,
      response: null,
      delivered: false,
    });
    return { ticket };
  }
  renew(id: string, ticketId: number): { renewed: boolean } {
    const worker = this.available(id);
    const ticket = this.ticket(ticketId, worker.id);
    if (!this.current(ticket) || !this.live(ticket.step.lease))
      return { renewed: false };
    ticket.step.lease!.expires = this.deadline();
    return { renewed: true };
  }
  call(
    id: string,
    ticketId: number,
  ):
    | { outcome: "stale" }
    | { kind: "execute" | "lookup"; outcome: ExecuteOutcome | LookupOutcome } {
    const worker = this.available(id);
    const ticket = this.ticket(ticketId, worker.id);
    // Fencing: only the step's current, live lease may reach the service.
    if (!this.current(ticket) || !this.live(ticket.step.lease))
      return { outcome: "stale" };
    if (ticket.response === null) {
      const key: ActionKey = [ticket.run.id, ticket.step.id];
      ticket.response =
        ticket.kind === "execute"
          ? this.service.execute(
              ticket.worker,
              ticket.id,
              key,
              ticket.definition.amount,
              ticket.attempt,
              ticket.definition.failures,
            )
          : this.service.lookup(ticket.worker, ticket.id, key, ticket.attempt);
    }
    // A repeat on the same live ticket replays the saved receipt unaudited.
    return { kind: ticket.kind, outcome: ticket.response };
  }
  deliver(ticketId: number): { committed: boolean } {
    const ticket = this.ticket(ticketId);
    if (
      ticket.response === null ||
      ticket.delivered ||
      !this.current(ticket) ||
      !this.live(ticket.step.lease) ||
      !this.worker(ticket.worker).up
    )
      return { committed: false };
    ticket.delivered = true;
    ticket.step.lease = null;
    this.commit(ticket);
    return { committed: true };
  }
  private commit(ticket: TicketRecord): void {
    const { run, step } = ticket;
    if (ticket.kind === "lookup") {
      // Reconciliation never starts work; an existing effect is never lost.
      if (ticket.response === "found") step.status = "succeeded";
      else step.status = run.status === "failing" ? "blocked" : "cancelled";
      if (!run.steps.some((other) => other.status === "running"))
        run.status = run.status === "failing" ? "failed" : "cancelled";
      return;
    }
    if (ticket.response !== "transient") {
      step.status = "succeeded";
      if (
        run.status === "active" &&
        run.steps.every((other) => other.status === "succeeded")
      )
        run.status = "succeeded";
      return;
    }
    if (step.attempts < this.workflow.maxAttempts) {
      // The retry deadline starts at delivery time, not at call time.
      require(
        this.now + this.workflow.retryDelay <= TIME_LIMIT,
        "TIME_OVERFLOW",
      );
      step.status = "pending";
      step.ready_at = this.now + this.workflow.retryDelay;
      return;
    }
    step.status = "failed";
    this.drain(run);
  }
  /** Exhaustion blocks pending work and fences any other running branch. */
  private drain(run: LeasedRunState): void {
    for (const step of run.steps)
      if (step.status === "pending") step.status = "blocked";
    const running = run.steps.filter((step) => step.status === "running");
    if (running.length === 0) {
      run.status = "failed";
      return;
    }
    run.status = "failing";
    for (const step of running) if (step.lease) step.lease.expires = this.now;
  }
  cancel(id: string): void {
    const run = this.runs.find((candidate) => candidate.id === id);
    require(run !== undefined, "UNKNOWN_RUN");
    if (run.status !== "active" && run.status !== "cancelling") return;
    const first = !run.cancel_requested;
    run.cancel_requested = true;
    for (const step of run.steps)
      if (step.status === "pending") step.status = "cancelled";
    // Only the first cancellation revokes leases: a repeat must not strand
    // a lookup ticket acquired by a recovering worker.
    if (first)
      for (const step of run.steps)
        if (step.status === "running" && step.lease)
          step.lease.expires = this.now;
    run.status = run.steps.some((step) => step.status === "running")
      ? "cancelling"
      : "cancelled";
  }
  apply(command: LeasedCommand): unknown {
    switch (command.op) {
      case "start": {
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
            lease: null,
          })),
        });
        return null;
      }
      case "advance":
        require(this.now + command.by <= TIME_LIMIT, "TIME_OVERFLOW");
        this.now += command.by; // Time alone changes no status and calls nothing.
        return null;
      case "observe":
        return this.snapshot();
      case "crash": {
        const worker = this.available(command.worker);
        worker.up = false; // Leases and undelivered responses are retained.
        return null;
      }
      case "restart": {
        const worker = this.worker(command.worker);
        require(!worker.up, "WORKER_UP");
        worker.up = true; // No recovery happens automatically.
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
      case "cancel":
        this.cancel(command.run);
        return null;
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
    for (const command of this.workflow.commands)
      this.results.push(this.apply(command));
    return {
      results: this.results,
      final: {
        ...this.snapshot(),
        calls: this.service.calls,
        effects: this.service.effects,
      },
    };
  }
}
/** Leased mode is selected by the presence of `workers`; otherwise the
 *  complete checkpoint-one contract applies unchanged. */
export function evaluateRequest(raw: unknown): SimulationResult | LeasedResult {
  if (
    raw !== null &&
    typeof raw === "object" &&
    !Array.isArray(raw) &&
    Object.hasOwn(raw, "workers")
  )
    return new LeasedSimulator(parseLeasedWorkflow(raw)).run();
  return new Simulator(parseWorkflow(raw)).run();
}
