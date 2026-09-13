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

// Checkpoint two deliberately has its own model: the old single-process model
// above remains byte-for-byte compatible when `workers` is absent.
type LStepStatus = StepStatus;
type LRunStatus = RunStatus | "failing";
interface Lease { worker: string; ticket: number; expires: number; }
interface LStep { id: string; status: LStepStatus; attempts: number; ready_at: number; lease: Lease | null; }
interface LRun { id: string; status: LRunStatus; cancel_requested: boolean; steps: LStep[]; }
type Receipt = { kind: "execute" | "lookup"; outcome: "applied" | "replayed" | "transient" | "found" | "missing" };
interface Ticket { id: number; worker: string; run: LRun; step: LStep; def: StepDefinition; kind: "execute" | "lookup"; receipt?: Receipt; delivered: boolean; }
export interface LeasedWorkflow { steps: readonly StepDefinition[]; commands: Record<string, unknown>[]; workers: string[]; maxAttempts: number; retryDelay: number; leaseDuration: number; }

function leasedCommand(raw: unknown): Record<string, unknown> {
  require(raw !== null && typeof raw === "object" && !Array.isArray(raw)); const o = raw as Record<string, unknown>;
  const op = o.op;
  if (op === "start" || op === "cancel") { fields(raw, ["op", "run"]); identifier(o.run); }
  else if (op === "advance") { fields(raw, ["op", "by"]); integer(o.by, 0, TIME_LIMIT); }
  else if (op === "observe") fields(raw, ["op"]);
  else if (op === "crash" || op === "restart") { fields(raw, ["op", "worker"]); identifier(o.worker); }
  else if (op === "claim") { fields(raw, ["op", "worker"]); identifier(o.worker); }
  else if (op === "renew" || op === "call") { fields(raw, ["op", "worker", "ticket"]); identifier(o.worker); integer(o.ticket, 1, TIME_LIMIT); }
  else if (op === "deliver") { fields(raw, ["op", "ticket"]); integer(o.ticket, 1, TIME_LIMIT); }
  else throw new DomainError("INVALID_INPUT");
  return o;
}
export function parseLeasedWorkflow(raw: unknown): LeasedWorkflow {
  const o = fields(raw, ["steps", "commands", "workers"], ["max_attempts", "retry_delay", "lease_duration"]);
  require(Array.isArray(o.steps) && o.steps.length && Array.isArray(o.commands) && o.commands.length <= 2000 && Array.isArray(o.workers) && o.workers.length > 0 && o.workers.length <= 100);
  const workers = o.workers.map(identifier); require(new Set(workers).size === workers.length);
  const w = { steps: o.steps.map(parseStep), commands: o.commands.map(leasedCommand), workers,
    maxAttempts: integer(Object.hasOwn(o,"max_attempts") ? o.max_attempts : 3,1,10), retryDelay: integer(Object.hasOwn(o,"retry_delay") ? o.retry_delay : 2,1,1_000_000), leaseDuration: integer(Object.hasOwn(o,"lease_duration") ? o.lease_duration : 5,1,1_000_000) };
  validateGraph(w.steps); return w;
}

export class LeasedSimulator {
  now=0; readonly runs:LRun[]=[]; readonly results:unknown[]=[]; readonly tickets=new Map<number,Ticket>(); nextTicket=1;
  readonly workers: {id:string;up:boolean}[]; readonly calls:unknown[]=[]; readonly effects:unknown[]=[];
  private readonly effectMap=new Map<string, Set<string>>(); private readonly counts=new Map<string,number>();
  readonly workflow: LeasedWorkflow;
  constructor(workflow:LeasedWorkflow) { this.workflow=workflow; this.workers=workflow.workers.map(id=>({id,up:true})); }
  private worker(id:string, needed=true) { const w=this.workers.find(x=>x.id===id); require(w,"UNKNOWN_WORKER"); if(needed) require(w.up,"WORKER_DOWN"); return w!; }
  private key(t:Ticket): [string,string] { return [t.run.id,t.step.id]; }
  private has(key:[string,string]) { return this.effectMap.get(key[0])?.has(key[1]) ?? false; }
  private live(t:Ticket) { return t.step.lease?.ticket===t.id && this.now<t.step.lease.expires; }
  private overflow(n:number) { require(n<=TIME_LIMIT,"TIME_OVERFLOW"); return n; }
  private lookupKind(r:LRun) { return r.status === "cancelling" || r.status === "failing" ? "lookup" as const : "execute" as const; }
  private action(r:LRun,i:number) { return {run:r,step:r.steps[i]!,def:this.workflow.steps[i]!}; }
  private claim(worker:string): unknown {
    const w=this.worker(worker); if(this.ticketsLiveFor(worker)) throw new DomainError("WORKER_BUSY");
    let a: ReturnType<LeasedSimulator["action"]>|undefined;
    for(const r of this.runs) { const i=r.steps.findIndex(s=>s.status==="running" && s.lease!==null && this.now>=s.lease.expires); if(i>=0){a=this.action(r,i);break;} }
    if(!a) for(const r of this.runs) if(r.status==="active") { const good=new Set(r.steps.filter(s=>s.status==="succeeded").map(s=>s.id)); for(let i=0;i<r.steps.length;i++){const s=r.steps[i]!,d=this.workflow.steps[i]!;if(s.status==="pending"&&s.ready_at<=this.now&&d.needs.every(x=>good.has(x))){a=this.action(r,i);break;}} if(a)break; }
    if(!a)return {ticket:null};
    const id=this.nextTicket++; const expires=this.overflow(this.now+this.workflow.leaseDuration); if(a.step.status==="pending"){a.step.status="running";a.step.attempts++;}
    a.step.lease={worker:w.id,ticket:id,expires}; const t:Ticket={id,worker:w.id,...a,kind:this.lookupKind(a.run),delivered:false}; this.tickets.set(id,t); return {ticket:id};
  }
  private ticketsLiveFor(worker:string) { for(const t of this.tickets.values()) if(t.worker===worker&&this.live(t)) return true; return false; }
  private call(worker:string,id:number):unknown { this.worker(worker); const t=this.ticketFor(worker,id); if(!this.live(t))return {outcome:"stale"}; if(t.receipt)return {...t.receipt};
    const key=this.key(t); let receipt:Receipt;
    if(t.kind==="lookup") receipt={kind:"lookup",outcome:this.has(key)?"found":"missing"};
    else if(this.has(key)) receipt={kind:"execute",outcome:"replayed"};
    else { const n=(this.counts.get(key.join("\u0000"))??0)+1; this.counts.set(key.join("\u0000"),n); if(n<=t.def.failures) receipt={kind:"execute",outcome:"transient"}; else { receipt={kind:"execute",outcome:"applied"}; let set=this.effectMap.get(key[0]);if(!set){set=new Set;this.effectMap.set(key[0],set);}set.add(key[1]);this.effects.push({key,amount:t.def.amount}); } }
    t.receipt=receipt; this.calls.push({worker,ticket:id,kind:receipt.kind,key,attempt:t.step.attempts,outcome:receipt.outcome}); return {...receipt}; }
  private ticketFor(worker:string,id:number) { const t=this.tickets.get(id);require(t,"UNKNOWN_TICKET");require(t.worker===worker,"WRONG_WORKER");return t!; }
  private finishRun(r:LRun) { if(r.status==="cancelling" && !r.steps.some(s=>s.status==="running"))r.status="cancelled"; if(r.status==="failing"&&!r.steps.some(s=>s.status==="running"))r.status="failed"; if(r.status==="active"&&r.steps.every(s=>s.status==="succeeded"))r.status="succeeded"; }
  private fail(r:LRun,s:LStep) { s.status="failed";for(const x of r.steps)if(x.status==="pending")x.status="blocked";if(r.steps.some(x=>x.status==="running")){r.status="failing";for(const x of r.steps)if(x.status==="running"&&x.lease)x.lease.expires=this.now;}else r.status="failed"; }
  private deliver(id:number):unknown { const t=this.tickets.get(id);require(t,"UNKNOWN_TICKET"); if(!t.receipt||t.delivered||!this.live(t)||!this.worker(t.worker,false).up)return {committed:false};
    const out=t.receipt.outcome; t.delivered=true;t.step.lease=null;
    if(t.kind==="lookup"){t.step.status=out==="found"?"succeeded":t.run.status==="cancelling"?"cancelled":"blocked";this.finishRun(t.run);return {committed:true};}
    if(out==="transient"){if(t.step.attempts<this.workflow.maxAttempts){t.step.status="pending";t.step.ready_at=this.overflow(this.now+this.workflow.retryDelay);}else this.fail(t.run,t.step);}else {t.step.status="succeeded";this.finishRun(t.run);} return {committed:true}; }
  private cancel(id:string) { const r=this.runs.find(x=>x.id===id);require(r,"UNKNOWN_RUN");if(r.status==="succeeded"||r.status==="failed"||r.status==="cancelled"||r.status==="failing")return;if(r.status==="cancelling")return;r.cancel_requested=true;for(const s of r.steps){if(s.status==="pending")s.status="cancelled";if(s.status==="running"&&s.lease)s.lease.expires=this.now;}r.status=r.steps.some(s=>s.status==="running")?"cancelling":"cancelled"; }
  private snapshot(){return {now:this.now,workers:this.workers.map(w=>({...w})),runs:this.runs.map(r=>({id:r.id,status:r.status,cancel_requested:r.cancel_requested,steps:r.steps.map(s=>({...s,lease:s.lease?{...s.lease}:null}))}))};}
  private apply(o:Record<string,unknown>):unknown { switch(o.op) { case "start": {const id=o.run as string;require(!this.runs.some(r=>r.id===id),"DUPLICATE_RUN");this.runs.push({id,status:"active",cancel_requested:false,steps:this.workflow.steps.map(s=>({id:s.id,status:"pending",attempts:0,ready_at:this.now,lease:null}))});return null;} case "advance":this.now=this.overflow(this.now+(o.by as number));return null;case "observe":return this.snapshot();case "crash":{this.worker(o.worker as string);this.worker(o.worker as string).up=false;return null;}case "restart":{const w=this.worker(o.worker as string,false);require(!w.up,"WORKER_UP");w.up=true;return null;}case "claim":return this.claim(o.worker as string);case "renew":{this.worker(o.worker as string);const t=this.ticketFor(o.worker as string,o.ticket as number);if(this.live(t)){t.step.lease!.expires=this.overflow(this.now+this.workflow.leaseDuration);return {renewed:true};}return {renewed:false};}case "call":return this.call(o.worker as string,o.ticket as number);case "deliver":return this.deliver(o.ticket as number);case "cancel":this.cancel(o.run as string);return null;} }
  run(){for(const c of this.workflow.commands)this.results.push(this.apply(c));return {results:this.results,final:{...this.snapshot(),calls:this.calls,effects:this.effects}};}
}
