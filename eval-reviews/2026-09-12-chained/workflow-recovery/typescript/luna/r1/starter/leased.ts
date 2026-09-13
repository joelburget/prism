import { DomainError, TIME_LIMIT, StepDefinition, parseWorkflow } from "./workflow.ts";

type Status = "pending"|"running"|"succeeded"|"failed"|"blocked"|"cancelled";
type RunStatus = "active"|"succeeded"|"failed"|"failing"|"cancelling"|"cancelled";
type Key = readonly [string,string];
const need = (x: unknown, code="INVALID_INPUT"): asserts x => { if (!x) throw new DomainError(code); };
const obj = (x: unknown): Record<string,unknown> => { need(x !== null && typeof x === "object" && !Array.isArray(x)); return x as Record<string,unknown>; };
const fields = (x: unknown, req: string[], opt: string[]=[]): Record<string,unknown> => {
  const o=obj(x); need(req.every(k=>Object.hasOwn(o,k))); need(Object.keys(o).every(k=>req.includes(k)||opt.includes(k))); return o;
};
const int = (x:unknown, lo:number, hi:number):number => { need(typeof x === "number" && Number.isInteger(x) && x>=lo && x<=hi); return x as number; };
const ident = (x:unknown):string => { need(typeof x === "string" && /^[A-Za-z0-9_-]{1,64}$/.test(x)); return x; };

export interface LeasedWorkflow { steps: readonly StepDefinition[]; commands: LCommand[]; workers: string[]; maxAttempts:number; retryDelay:number; leaseDuration:number; }
type LCommand =
 | {op:"start"|"cancel";run:string} | {op:"advance";by:number} | {op:"observe"}
 | {op:"crash"|"restart"|"claim";worker:string} | {op:"renew"|"call";worker:string;ticket:number}
 | {op:"deliver";ticket:number};

function command(x:unknown):LCommand {
 const o=obj(x); const op=o.op;
 if(op==="start"||op==="cancel") { fields(x,["op","run"]); return {op,run:ident(o.run)}; }
 if(op==="advance") { fields(x,["op","by"]); return {op,by:int(o.by,0,TIME_LIMIT)}; }
 if(op==="observe") { fields(x,["op"]); return {op}; }
 if(op==="crash"||op==="restart"||op==="claim") { fields(x,["op","worker"]); return {op,worker:ident(o.worker)}; }
 if(op==="renew"||op==="call") { fields(x,["op","worker","ticket"]); return {op,worker:ident(o.worker),ticket:int(o.ticket,1,TIME_LIMIT)}; }
 if(op==="deliver") { fields(x,["op","ticket"]); return {op,ticket:int(o.ticket,1,TIME_LIMIT)}; }
 throw new DomainError("INVALID_INPUT");
}
export function parseLeasedWorkflow(raw:unknown):LeasedWorkflow {
 const o=fields(raw,["steps","commands","workers"],["max_attempts","retry_delay","lease_duration"]);
 need(Array.isArray(o.workers)&&o.workers.length>0); const workers=o.workers.map(ident); need(new Set(workers).size===workers.length);
 need(Array.isArray(o.commands));
 // Reuse the frozen parser for step scalar and graph validation.
 const base = JSON.parse(JSON.stringify({steps:o.steps,commands:[],max_attempts:o.max_attempts,retry_delay:o.retry_delay}));
 const parsed = parseWorkflow(base);
 return {steps:parsed.steps,commands:o.commands.map(command),workers,maxAttempts:parsed.maxAttempts,retryDelay:parsed.retryDelay,leaseDuration:int(Object.hasOwn(o,"lease_duration")?o.lease_duration:5,1,1_000_000)};
}
interface Step {id:string;status:Status;attempts:number;ready_at:number;lease:Lease|null}
interface Run {id:string;status:RunStatus;cancel_requested:boolean;steps:Step[]}
interface Lease {worker:string;ticket:number;expires:number}
interface Ticket {id:number;run:Run;step:Step;kind:"execute"|"lookup";lease:Lease;response?:Response;delivered:boolean}
type Response={kind:"execute"|"lookup";outcome:"applied"|"replayed"|"transient"|"found"|"missing"};
interface Call extends Response {worker:string;ticket:number;key:Key;attempt:number}
interface Effect {key:Key;amount:number}

export class LeasedSimulator {
 now=0; readonly runs:Run[]=[]; readonly up=new Map<string,boolean>(); readonly tickets=new Map<number,Ticket>(); nextTicket=1;
 readonly calls:Call[]=[]; readonly effects:Effect[]=[]; readonly failures=new Map<string,number>(); readonly observations:unknown[]=[];
 constructor(readonly workflow:LeasedWorkflow){workflow.workers.forEach(w=>this.up.set(w,true));}
 private checkWorker(w:string, available=true){need(this.up.has(w),"UNKNOWN_WORKER"); if(available) need(this.up.get(w),"WORKER_DOWN");}
 private ticket(n:number,w?:string):Ticket { const t=this.tickets.get(n); need(t,"UNKNOWN_TICKET"); if(w!==undefined) need(t.lease.worker===w,"WRONG_WORKER"); return t; }
 private live(t:Ticket){return t.step.lease?.ticket===t.id && t.step.lease.worker===t.lease.worker && this.now<t.lease.expires;}
 private key(t:Ticket):Key{return [t.run.id,t.step.id];}
 private select():{run:Run;step:Step;kind:"execute"|"lookup"}|undefined {
  for(const r of this.runs) for(const s of r.steps) if(s.status==="running" && s.lease!==null && this.now>=s.lease.expires) return {run:r,step:s,kind:r.status==="active"?"execute":"lookup"};
  for(const r of this.runs) { if(r.status!=="active") continue; const done=new Set(r.steps.filter(s=>s.status==="succeeded").map(s=>s.id)); for(const [i,s] of r.steps.entries()) if(s.status==="pending"&&s.ready_at<=this.now&&this.workflow.steps[i]!.needs.every(d=>done.has(d))) return {run:r,step:s,kind:"execute"}; }
 }
 private snapshot(){return {now:this.now,workers:this.workflow.workers.map(id=>({id,up:this.up.get(id)!})),runs:this.runs.map(r=>({...r,steps:r.steps.map(s=>({...s,lease:s.lease?{...s.lease}:null}))}))};}
 private service(t:Ticket):Response { const key=this.key(t), k=JSON.stringify(key); let out:Response['outcome']; if(t.kind==="lookup"){out=this.effects.some(e=>JSON.stringify(e.key)===k)?"found":"missing";}
  else if(this.effects.some(e=>JSON.stringify(e.key)===k)) out="replayed"; else {const n=this.failures.get(k)??0; const def=this.workflow.steps[this.workflow.steps.findIndex(s=>s.id===t.step.id)]!; if(n<def.failures){this.failures.set(k,n+1);out="transient";}else{out="applied";this.effects.push({key:[...key],amount:def.amount});}}
  const response={kind:t.kind,outcome:out}; this.calls.push({worker:t.lease.worker,ticket:t.id,key:[...key],attempt:t.step.attempts,...response}); return response;
 }
 private commit(t:Ticket):boolean { if(!t.response||t.delivered||!this.live(t)||!this.up.get(t.lease.worker)) return false; const r=t.run,s=t.step; const out=t.response.outcome; s.lease=null;t.delivered=true;
  if(t.kind==="lookup"){s.status=out==="found"?"succeeded":(r.status==="failing"?"blocked":"cancelled"); if(r.status==="cancelling") {if(!r.steps.some(x=>x.status==="running"))r.status="cancelled";} else if(r.status==="failing"&&!r.steps.some(x=>x.status==="running"))r.status="failed"; return true;}
  if(out==="transient"){if(s.attempts<this.workflow.maxAttempts){need(this.now+this.workflow.retryDelay<=TIME_LIMIT,"TIME_OVERFLOW");s.status="pending";s.ready_at=this.now+this.workflow.retryDelay;}else{ s.status="failed";r.steps.forEach(x=>{if(x.status==="pending")x.status="blocked"});r.status="failing";r.steps.forEach(x=>{if(x.status==="running"&&x!==s&&x.lease)x.lease.expires=this.now;}); if(!r.steps.some(x=>x.status==="running"))r.status="failed"; }} else {s.status="succeeded";if(r.steps.every(x=>x.status==="succeeded"))r.status="succeeded";} return true; }
 apply(c:LCommand):unknown { switch(c.op){
  case"start": need(this.runs.every(r=>r.id!==c.run),"DUPLICATE_RUN");this.runs.push({id:c.run,status:"active",cancel_requested:false,steps:this.workflow.steps.map(s=>({id:s.id,status:"pending",attempts:0,ready_at:this.now,lease:null}))});return null;
  case"advance":need(this.now+c.by<=TIME_LIMIT,"TIME_OVERFLOW");this.now+=c.by;return null;
  case"observe":return this.snapshot();
  case"crash":this.checkWorker(c.worker);this.up.set(c.worker,false);return null;
  case"restart":this.checkWorker(c.worker,false);need(!this.up.get(c.worker),"WORKER_UP");this.up.set(c.worker,true);return null;
  case"claim":this.checkWorker(c.worker); for(const t of this.tickets.values())if(t.lease.worker===c.worker&&this.live(t))throw new DomainError("WORKER_BUSY"); {const a=this.select();if(!a)return {ticket:null};const ticket={worker:c.worker,ticket:this.nextTicket++,expires:this.now+this.workflow.leaseDuration};need(ticket.expires<=TIME_LIMIT,"TIME_OVERFLOW");if(a.step.status==="pending"){a.step.status="running";a.step.attempts++;}a.step.lease=ticket;const t={id:ticket.ticket,run:a.run,step:a.step,kind:a.kind,lease:ticket,delivered:false};this.tickets.set(t.id,t);return {ticket:t.id};}
  case"renew":this.checkWorker(c.worker);{const t=this.ticket(c.ticket,c.worker);if(this.live(t)){need(this.now+this.workflow.leaseDuration<=TIME_LIMIT,"TIME_OVERFLOW");t.lease.expires=this.now+this.workflow.leaseDuration;t.step.lease={...t.lease};return {renewed:true};}return {renewed:false};}
  case"call":this.checkWorker(c.worker);{const t=this.ticket(c.ticket,c.worker);if(!this.live(t))return {outcome:"stale"};if(!t.response)t.response=this.service(t);return {kind:t.response.kind,outcome:t.response.outcome};}
  case"deliver":{const t=this.ticket(c.ticket);return {committed:this.commit(t)};}
  case"cancel":{const r=this.runs.find(x=>x.id===c.run);need(r,"UNKNOWN_RUN");if(["succeeded","failed","cancelled","failing"].includes(r.status))return null;if(r.status==="cancelling")return null;r.cancel_requested=true;r.steps.forEach(s=>{if(s.status==="pending")s.status="cancelled";if(s.status==="running"&&s.lease)s.lease.expires=this.now;});r.status=r.steps.some(s=>s.status==="running")?"cancelling":"cancelled";return null;}
 }}
 run(){const results=this.workflow.commands.map(c=>this.apply(c));return {results,final:{...this.snapshot(),calls:this.calls.map(c=>({...c,key:[...c.key]})),effects:this.effects.map(e=>({...e,key:[...e.key]}))}};}
}
