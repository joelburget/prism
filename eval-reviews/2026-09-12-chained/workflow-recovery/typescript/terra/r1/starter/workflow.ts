/** Deterministic durable, in-memory workflow runner. */
export const TIME_LIMIT = 2_147_483_647;
export class DomainError extends Error { readonly code: string; constructor(code: string) { super(code); this.code = code; } }
function require(condition: unknown, code = "INVALID_INPUT"): asserts condition { if (!condition) throw new DomainError(code); }
function fields(value: unknown, required: string[], optional: string[] = []): Record<string, unknown> {
  require(value !== null && typeof value === "object" && !Array.isArray(value));
  const object = value as Record<string, unknown>;
  require(required.every(k => Object.hasOwn(object, k)) && Object.keys(object).every(k => required.includes(k) || optional.includes(k)));
  return object;
}
function integer(value: unknown, low: number, high: number): number {
  require(typeof value === "number" && Number.isInteger(value) && value >= low && value <= high);
  return value;
}
function identifier(value: unknown): string { require(typeof value === "string" && /^[A-Za-z0-9_-]{1,64}$/.test(value)); return value; }

export interface StepDefinition { readonly id: string; readonly needs: readonly string[]; readonly amount: number; readonly failures: number; }
export type Command =
  | { op: "start" | "cancel"; run: string }
  | { op: "advance"; by: number }
  | { op: "tick"; crashAt: "after_begin" | "after_call" | null }
  | { op: "observe" | "crash" | "restart" };
export interface Workflow { readonly steps: readonly StepDefinition[]; readonly commands: readonly Command[]; readonly maxAttempts: number; readonly retryDelay: number; }
function parseStep(raw: unknown): StepDefinition {
  const o = fields(raw, ["id", "needs", "amount"], ["failures"]);
  require(Array.isArray(o.needs)); const needs = o.needs.map(identifier); require(new Set(needs).size === needs.length);
  return { id: identifier(o.id), needs, amount: integer(o.amount, 1, 1_000_000), failures: integer(Object.hasOwn(o, "failures") ? o.failures : 0, 0, 100) };
}
function parseCommand(raw: unknown): Command {
  require(raw !== null && typeof raw === "object" && !Array.isArray(raw)); const o = raw as Record<string, unknown>;
  if (o.op === "start" || o.op === "cancel") { fields(raw, ["op", "run"]); return { op: o.op, run: identifier(o.run) }; }
  if (o.op === "advance") { fields(raw, ["op", "by"]); return { op: "advance", by: integer(o.by, 0, TIME_LIMIT) }; }
  if (o.op === "tick") { fields(raw, ["op"], ["crash_at"]); const c = Object.hasOwn(o, "crash_at") ? o.crash_at : null; require(c === null || c === "after_begin" || c === "after_call"); return { op: "tick", crashAt: c }; }
  if (o.op === "observe" || o.op === "crash" || o.op === "restart") { fields(raw, ["op"]); return { op: o.op }; }
  throw new DomainError("INVALID_INPUT");
}
function validateGraph(steps: readonly StepDefinition[]): void {
  const ids = new Set(steps.map(s => s.id)); require(ids.size === steps.length, "DUPLICATE_STEP");
  require(steps.every(s => s.needs.every(d => ids.has(d))), "UNKNOWN_DEPENDENCY");
  const done = new Set<string>(); let todo = [...steps];
  while (todo.length) { const ready = todo.filter(s => s.needs.every(d => done.has(d))); require(ready.length, "DEPENDENCY_CYCLE"); ready.forEach(s => done.add(s.id)); todo = todo.filter(s => !done.has(s.id)); }
}
export function parseWorkflow(raw: unknown): Workflow {
  const o = fields(raw, ["steps", "commands"], ["max_attempts", "retry_delay"]); require(Array.isArray(o.steps) && o.steps.length && Array.isArray(o.commands));
  const w: Workflow = { steps: o.steps.map(parseStep), commands: o.commands.map(parseCommand), maxAttempts: integer(Object.hasOwn(o, "max_attempts") ? o.max_attempts : 3, 1, 10), retryDelay: integer(Object.hasOwn(o, "retry_delay") ? o.retry_delay : 2, 1, 1_000_000) };
  validateGraph(w.steps); return w;
}

type StepStatus = "pending" | "running" | "succeeded" | "failed" | "blocked" | "cancelled";
type RunStatus = "active" | "succeeded" | "failed" | "cancelling" | "cancelled";
export interface StepState { id: string; status: StepStatus; attempts: number; ready_at: number; }
export interface RunState { id: string; status: RunStatus; cancel_requested: boolean; steps: StepState[]; }
type ActionKey = readonly [string, string];
interface ServiceCall { kind: "execute" | "lookup"; key: ActionKey; attempt: number; outcome: "applied" | "replayed" | "transient" | "found" | "missing"; }
interface Effect { key: ActionKey; amount: number; }
type ExecuteOutcome = "applied" | "replayed" | "transient";

export class MockService {
  readonly calls: ServiceCall[] = []; readonly effects: Effect[] = [];
  private readonly effectsByRun = new Map<string, Map<string, Effect>>();
  private readonly executeCounts = new Map<string, Map<string, number>>();
  private effect(key: ActionKey): Effect | undefined { return this.effectsByRun.get(key[0])?.get(key[1]); }
  execute(key: ActionKey, amount: number, attempt: number, failures: number): ExecuteOutcome {
    let outcome: ExecuteOutcome;
    if (this.effect(key)) outcome = "replayed";
    else { const counts = this.executeCounts.get(key[0]) ?? new Map<string, number>(); this.executeCounts.set(key[0], counts); const n = (counts.get(key[1]) ?? 0) + 1; counts.set(key[1], n);
      if (n <= failures) outcome = "transient";
      else { outcome = "applied"; const effect: Effect = { key: [key[0], key[1]], amount }; const effects = this.effectsByRun.get(key[0]) ?? new Map<string, Effect>(); this.effectsByRun.set(key[0], effects); effects.set(key[1], effect); this.effects.push(effect); }
    }
    this.calls.push({ kind: "execute", key: [key[0], key[1]], attempt, outcome }); return outcome;
  }
  lookup(key: ActionKey, attempt: number): "found" | "missing" { const outcome = this.effect(key) ? "found" : "missing"; this.calls.push({ kind: "lookup", key: [key[0], key[1]], attempt, outcome }); return outcome; }
}
export interface Snapshot { now: number; up: boolean; runs: RunState[]; }
export interface SimulationResult { observations: Snapshot[]; final: Snapshot & { calls: ServiceCall[]; effects: Effect[] }; }
interface Action { run: RunState; step: StepState; definition: StepDefinition; }

export class Simulator {
  now = 0; up = true; readonly runs: RunState[] = []; readonly service = new MockService(); readonly observations: Snapshot[] = [];
  readonly workflow: Workflow;
  constructor(workflow: Workflow) { this.workflow = workflow; }
  private action(run: RunState, index: number): Action { return { run, step: run.steps[index]!, definition: this.workflow.steps[index]! }; }
  selectAction(): Action | undefined {
    for (const run of this.runs) { const i = run.steps.findIndex(s => s.status === "running"); if (i >= 0) return this.action(run, i); }
    for (const run of this.runs) if (run.status === "active") {
      const succeeded = new Set(run.steps.filter(s => s.status === "succeeded").map(s => s.id));
      for (let i = 0; i < run.steps.length; i++) { const step = run.steps[i]!, def = this.workflow.steps[i]!; if (step.status === "pending" && step.ready_at <= this.now && def.needs.every(d => succeeded.has(d))) return this.action(run, i); }
    }
    return undefined;
  }
  private checkpoint(crashAt: "after_begin" | "after_call" | null): boolean { if (crashAt === "after_begin") { this.up = false; return true; } return false; }
  private completeSuccess(run: RunState, step: StepState): void { step.status = "succeeded"; if (run.steps.every(s => s.status === "succeeded")) run.status = "succeeded"; }
  private transient(run: RunState, step: StepState): void {
    if (step.attempts < this.workflow.maxAttempts) { const deadline = this.now + this.workflow.retryDelay; require(deadline <= TIME_LIMIT, "TIME_OVERFLOW"); step.status = "pending"; step.ready_at = deadline; }
    else { step.status = "failed"; run.status = "failed"; for (const other of run.steps) if (other.status === "pending") other.status = "blocked"; }
  }
  tick(crashAt: "after_begin" | "after_call" | null): void {
    const a = this.selectAction(); if (!a) return; const { run, step, definition } = a;
    if (run.status === "cancelling") {
      if (this.checkpoint(crashAt)) return;
      const outcome = this.service.lookup([run.id, step.id], step.attempts);
      if (crashAt === "after_call") { this.up = false; return; }
      step.status = outcome === "found" ? "succeeded" : "cancelled"; run.status = "cancelled"; return;
    }
    if (step.status === "pending") { step.status = "running"; step.attempts++; }
    if (this.checkpoint(crashAt)) return;
    const outcome = this.service.execute([run.id, step.id], definition.amount, step.attempts, definition.failures);
    if (crashAt === "after_call") { this.up = false; return; }
    if (outcome === "transient") this.transient(run, step); else this.completeSuccess(run, step);
  }
  private cancel(id: string): void {
    const run = this.runs.find(r => r.id === id); require(run, "UNKNOWN_RUN");
    if (run.status === "succeeded" || run.status === "failed" || run.status === "cancelled") return;
    run.cancel_requested = true; for (const step of run.steps) if (step.status === "pending") step.status = "cancelled";
    run.status = run.steps.some(s => s.status === "running") ? "cancelling" : "cancelled";
  }
  apply(command: Command): void {
    if (command.op === "observe") { this.observations.push(this.snapshot()); return; }
    if (command.op === "advance") { require(this.now + command.by <= TIME_LIMIT, "TIME_OVERFLOW"); this.now += command.by; return; }
    if (command.op === "restart") { require(!this.up, "PROCESS_UP"); this.up = true; return; }
    require(this.up, "PROCESS_DOWN");
    if (command.op === "crash") { this.up = false; return; }
    if (command.op === "start") { require(!this.runs.some(r => r.id === command.run), "DUPLICATE_RUN"); this.runs.push({ id: command.run, status: "active", cancel_requested: false, steps: this.workflow.steps.map(s => ({ id: s.id, status: "pending", attempts: 0, ready_at: this.now })) }); return; }
    if (command.op === "cancel") { this.cancel(command.run); return; }
    if (command.op === "tick") this.tick(command.crashAt);
  }
  snapshot(): Snapshot { return { now: this.now, up: this.up, runs: this.runs.map(r => ({ ...r, steps: r.steps.map(s => ({ ...s })) })) }; }
  run(): SimulationResult { for (const command of this.workflow.commands) this.apply(command); return { observations: this.observations, final: { ...this.snapshot(), calls: this.service.calls, effects: this.service.effects } }; }
}
