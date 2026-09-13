"""Deterministic durable workflow simulators (original and leased-worker modes)."""
from dataclasses import dataclass, field
import re
from typing import Any

TIME_LIMIT = 2_147_483_647


class DomainError(Exception):
    def __init__(self, code: str): self.code = code; super().__init__(code)


def require(ok: bool, code: str = "INVALID_INPUT") -> None:
    if not ok: raise DomainError(code)


def fields(v: Any, required: set[str], optional: set[str] = frozenset()) -> dict:
    require(type(v) is dict and required <= v.keys() <= required | optional)
    return v


def integer(v: Any, low: int, high: int) -> int:
    require(type(v) is int and low <= v <= high)
    return v


def ident(v: Any) -> str:
    require(type(v) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", v) is not None)
    return v


@dataclass(frozen=True)
class StepDef:
    id: str; needs: tuple[str, ...]; amount: int; failures: int = 0


def parse_step(v: Any) -> StepDef:
    o = fields(v, {"id", "needs", "amount"}, {"failures"})
    require(type(o["needs"]) is list)
    needs = tuple(ident(x) for x in o["needs"])
    require(len(needs) == len(set(needs)))
    return StepDef(ident(o["id"]), needs, integer(o["amount"], 1, 1_000_000),
                   integer(o.get("failures", 0), 0, 100))


def validate_graph(steps: tuple[StepDef, ...]) -> None:
    names = {x.id for x in steps}
    require(len(names) == len(steps), "DUPLICATE_STEP")
    require(all(n in names for x in steps for n in x.needs), "UNKNOWN_DEPENDENCY")
    seen: set[str] = set(); todo = list(steps)
    while todo:
        ready = [x for x in todo if set(x.needs) <= seen]
        require(bool(ready), "DEPENDENCY_CYCLE")
        seen.update(x.id for x in ready)
        todo = [x for x in todo if x.id not in seen]


@dataclass
class Step:
    id: str; status: str = "pending"; attempts: int = 0; ready_at: int = 0


@dataclass
class Run:
    id: str; steps: list[Step]; status: str = "active"; cancel_requested: bool = False


@dataclass
class Service:
    effects: list[tuple[tuple[str, str], int]] = field(default_factory=list)
    failures: dict[tuple[str, str], int] = field(default_factory=dict)

    def execute(self, key, amount, failures):
        if any(k == key for k, _ in self.effects): return "replayed"
        if self.failures.get(key, 0) < failures:
            self.failures[key] = self.failures.get(key, 0) + 1
            return "transient"
        self.effects.append((key, amount)); return "applied"

    def lookup(self, key):
        return "found" if any(k == key for k, _ in self.effects) else "missing"


# Checkpoint one (kept as the no-workers protocol).
@dataclass(frozen=True)
class OldCmd:
    op: str; run: str | None = None; by: int = 0; crash_at: str | None = None


def parse_old_cmd(v: Any) -> OldCmd:
    require(type(v) is dict and type(v.get("op")) is str); op = v["op"]
    if op in ("start", "cancel"):
        fields(v, {"op", "run"}); return OldCmd(op, ident(v["run"]))
    if op == "advance": fields(v, {"op", "by"}); return OldCmd(op, by=integer(v["by"], 0, TIME_LIMIT))
    if op == "tick":
        fields(v, {"op"}, {"crash_at"}); c = v.get("crash_at")
        if "crash_at" in v:
            require(type(c) is str and c in ("after_begin", "after_call"))
        return OldCmd(op, crash_at=c)
    require(op in ("observe", "crash", "restart")); fields(v, {"op"}); return OldCmd(op)


class OldSimulator:
    def __init__(self, steps, cmds, max_attempts, retry_delay):
        self.steps_def, self.cmds, self.max_attempts, self.retry_delay = steps, cmds, max_attempts, retry_delay
        self.now = 0; self.up = True; self.runs: list[Run] = []; self.service = Service(); self.observations = []; self.calls = []
    def select(self):
        for r in self.runs:
            for s, d in zip(r.steps, self.steps_def):
                if s.status == "running": return r, s, d
        for r in self.runs:
            if r.status == "active":
                done = {s.id for s in r.steps if s.status == "succeeded"}
                for s, d in zip(r.steps, self.steps_def):
                    if s.status == "pending" and s.ready_at <= self.now and all(x in done for x in d.needs): return r, s, d
        return None
    def finish_success(self, r, s):
        s.status = "succeeded"
        if all(x.status == "succeeded" for x in r.steps): r.status = "succeeded"
    def transient(self, r, s):
        if s.attempts < self.max_attempts:
            require(self.now + self.retry_delay <= TIME_LIMIT, "TIME_OVERFLOW")
            s.status = "pending"; s.ready_at = self.now + self.retry_delay
        else:
            s.status = "failed"; r.status = "failed"
            for x in r.steps:
                if x.status == "pending": x.status = "blocked"
    def tick(self, crash_at):
        a = self.select()
        if not a: return
        r, s, d = a
        if s.status == "pending": s.status = "running"; s.attempts += 1
        if crash_at == "after_begin": self.up = False; return
        key = (r.id, s.id)
        if r.status == "cancelling":
            out = self.service.lookup(key); self.calls.append(("lookup", key, s.attempts, out))
            if crash_at == "after_call": self.up = False; return
            s.status = "succeeded" if out == "found" else "cancelled"; r.status = "cancelled"; return
        out = self.service.execute(key, d.amount, d.failures); self.calls.append(("execute", key, s.attempts, out))
        if crash_at == "after_call": self.up = False; return
        self.finish_success(r, s) if out != "transient" else self.transient(r, s)
    def cancel(self, rid):
        r = next((x for x in self.runs if x.id == rid), None); require(r is not None, "UNKNOWN_RUN")
        if r.status in ("succeeded", "failed", "cancelled"): return
        r.cancel_requested = True
        for s in r.steps:
            if s.status == "pending": s.status = "cancelled"
        r.status = "cancelling" if any(s.status == "running" for s in r.steps) else "cancelled"
    def snapshot(self):
        return {"now": self.now, "up": self.up, "runs": [{"id": r.id, "status": r.status, "cancel_requested": r.cancel_requested, "steps": [{"id": s.id, "status": s.status, "attempts": s.attempts, "ready_at": s.ready_at} for s in r.steps]} for r in self.runs]}
    def apply(self, c):
        if c.op == "observe": self.observations.append(self.snapshot()); return
        if c.op == "advance": require(self.now + c.by <= TIME_LIMIT, "TIME_OVERFLOW"); self.now += c.by; return
        if c.op == "restart": require(not self.up, "PROCESS_UP"); self.up = True; return
        require(self.up, "PROCESS_DOWN")
        if c.op == "crash": self.up = False
        elif c.op == "start":
            require(all(r.id != c.run for r in self.runs), "DUPLICATE_RUN")
            self.runs.append(Run(c.run, [Step(d.id, ready_at=self.now) for d in self.steps_def]))
        elif c.op == "cancel": self.cancel(c.run)
        else: self.tick(c.crash_at)
    def run(self):
        for c in self.cmds: self.apply(c)
        final = self.snapshot(); final["calls"] = [{"kind": k, "key": list(key), "attempt": a, "outcome": o} for k,key,a,o in self.calls]
        final["effects"] = [{"key": list(k), "amount": a} for k,a in self.service.effects]
        return {"observations": self.observations, "final": final}


@dataclass
class Lease: worker: str; ticket: int; expires: int
@dataclass
class LStep(Step): lease: Lease | None = None
@dataclass
class Ticket: id: int; worker: str; run: Run; step: LStep; definition: StepDef; kind: str; response: str | None = None; delivered: bool = False


def parse_leased_cmd(v):
    require(type(v) is dict and type(v.get("op")) is str); op = v["op"]
    if op in ("start", "cancel"): fields(v, {"op", "run"}); return (op, ident(v["run"]))
    if op == "advance": fields(v, {"op", "by"}); return (op, integer(v["by"], 0, TIME_LIMIT))
    if op in ("observe",): fields(v, {"op"}); return (op,)
    if op in ("crash", "restart", "claim"): fields(v, {"op", "worker"}); return (op, ident(v["worker"]))
    if op in ("renew", "call"):
        fields(v, {"op", "worker", "ticket"}); return (op, ident(v["worker"]), integer(v["ticket"], 1, TIME_LIMIT))
    if op == "deliver": fields(v, {"op", "ticket"}); return (op, integer(v["ticket"], 1, TIME_LIMIT))
    raise DomainError("INVALID_INPUT")


class LeasedSimulator:
    def __init__(self, steps, cmds, workers, max_attempts, retry_delay, duration):
        self.defs, self.cmds, self.max_attempts, self.retry_delay, self.duration = steps, cmds, max_attempts, retry_delay, duration
        self.workers = {w: True for w in workers}; self.worker_order = workers; self.now = 0; self.runs = []; self.service = Service(); self.tickets = {}; self.next_ticket = 1; self.calls = []
    def worker(self, w, up=True):
        require(w in self.workers, "UNKNOWN_WORKER")
        if up: require(self.workers[w], "WORKER_DOWN")
    def ticket(self, n): require(n in self.tickets, "UNKNOWN_TICKET"); return self.tickets[n]
    def owned(self, w, n):
        self.worker(w); t = self.ticket(n); require(t.worker == w, "WRONG_WORKER"); return t
    def live(self, lease): return lease is not None and self.now < lease.expires
    def select(self):
        for r in self.runs:
            for s,d in zip(r.steps,self.defs):
                if s.status == "running" and not self.live(s.lease): return r,s,d
        for r in self.runs:
            if r.status == "active":
                done={s.id for s in r.steps if s.status=="succeeded"}
                for s,d in zip(r.steps,self.defs):
                    if s.status=="pending" and s.ready_at<=self.now and all(x in done for x in d.needs): return r,s,d
        return None
    def claim(self,w):
        self.worker(w)
        require(not any(s.status=="running" and self.live(s.lease) and s.lease.worker==w for r in self.runs for s in r.steps), "WORKER_BUSY")
        item=self.select()
        if not item:return {"ticket":None}
        r,s,d=item; require(self.now+self.duration<=TIME_LIMIT,"TIME_OVERFLOW")
        if s.status=="pending": s.status="running"; s.attempts+=1
        n=self.next_ticket; self.next_ticket+=1; s.lease=Lease(w,n,self.now+self.duration)
        kind="execute" if r.status=="active" else "lookup"
        self.tickets[n]=Ticket(n,w,r,s,d,kind); return {"ticket":n}
    def call(self,w,n):
        t=self.owned(w,n)
        if t.step.lease is None or t.step.lease.ticket != n or not self.live(t.step.lease): return {"outcome":"stale"}
        if t.response is None:
            key=(t.run.id,t.step.id)
            t.response=self.service.execute(key,t.definition.amount,t.definition.failures) if t.kind=="execute" else self.service.lookup(key)
            self.calls.append({"worker":w,"ticket":n,"kind":t.kind,"key":list(key),"attempt":t.step.attempts,"outcome":t.response})
        return {"kind":t.kind,"outcome":t.response}
    def settle_terminal(self,r):
        if r.status=="cancelling" and not any(s.status=="running" for s in r.steps): r.status="cancelled"
        if r.status=="failing" and not any(s.status=="running" for s in r.steps): r.status="failed"
    def deliver(self,n):
        t=self.ticket(n); s=t.step; lease=s.lease
        if t.response is None or t.delivered or lease is None or lease.ticket!=n or not self.live(lease) or not self.workers[t.worker]: return {"committed":False}
        t.delivered=True; s.lease=None; r=t.run; out=t.response
        if t.kind=="lookup":
            if r.status=="cancelling": s.status="succeeded" if out=="found" else "cancelled"
            else: s.status="succeeded" if out=="found" else "blocked"
            self.settle_terminal(r); return {"committed":True}
        if out in ("applied","replayed"):
            s.status="succeeded"
            if r.status=="active" and all(x.status=="succeeded" for x in r.steps): r.status="succeeded"
        elif s.attempts < self.max_attempts:
            require(self.now+self.retry_delay<=TIME_LIMIT,"TIME_OVERFLOW"); s.status="pending"; s.ready_at=self.now+self.retry_delay
        else:
            s.status="failed"
            for x in r.steps:
                if x.status=="pending": x.status="blocked"
            running=[x for x in r.steps if x.status=="running"]
            if running:
                r.status="failing"
                for x in running: x.lease.expires=self.now
            else:r.status="failed"
        return {"committed":True}
    def cancel(self,rid):
        r=next((x for x in self.runs if x.id==rid),None); require(r is not None,"UNKNOWN_RUN")
        if r.status in ("succeeded","failed","cancelled","failing","cancelling"): return
        r.cancel_requested=True
        for s in r.steps:
            if s.status=="pending":s.status="cancelled"
        running=[s for s in r.steps if s.status=="running"]
        if running:
            r.status="cancelling"
            for s in running:s.lease.expires=self.now
        else:r.status="cancelled"
    def snapshot(self):
        return {"now":self.now,"workers":[{"id":w,"up":self.workers[w]} for w in self.worker_order],"runs":[{"id":r.id,"status":r.status,"cancel_requested":r.cancel_requested,"steps":[{"id":s.id,"status":s.status,"attempts":s.attempts,"ready_at":s.ready_at,"lease":None if s.lease is None else {"worker":s.lease.worker,"ticket":s.lease.ticket,"expires":s.lease.expires}} for s in r.steps]} for r in self.runs]}
    def apply(self,c):
        op=c[0]
        if op=="observe":return self.snapshot()
        if op=="advance":require(self.now+c[1]<=TIME_LIMIT,"TIME_OVERFLOW");self.now+=c[1];return None
        if op=="start":
            require(all(r.id!=c[1] for r in self.runs),"DUPLICATE_RUN");self.runs.append(Run(c[1],[LStep(d.id,ready_at=self.now) for d in self.defs]));return None
        if op=="cancel":self.cancel(c[1]);return None
        if op=="crash":self.worker(c[1]);self.workers[c[1]]=False;return None
        if op=="restart":
            require(c[1] in self.workers,"UNKNOWN_WORKER");require(not self.workers[c[1]],"WORKER_UP");self.workers[c[1]]=True;return None
        if op=="claim":return self.claim(c[1])
        if op=="renew":
            t=self.owned(c[1],c[2]); s=t.step
            if s.lease is not None and s.lease.ticket==t.id and self.live(s.lease):
                require(self.now+self.duration<=TIME_LIMIT,"TIME_OVERFLOW");s.lease.expires=self.now+self.duration;return {"renewed":True}
            return {"renewed":False}
        if op=="call":return self.call(c[1],c[2])
        return self.deliver(c[1])
    def run(self):
        values=[self.apply(c) for c in self.cmds]; final=self.snapshot();final["calls"]=self.calls;final["effects"]=[{"key":list(k),"amount":a} for k,a in self.service.effects]
        return {"results":values,"final":final}


def parse_workflow(raw: Any):
    require(type(raw) is dict)
    leased="workers" in raw
    allowed={"steps","commands","max_attempts","retry_delay"} | ({"workers","lease_duration"} if leased else set())
    fields(raw,{"steps","commands"} | ({"workers"} if leased else set()),allowed-({"steps","commands"} | ({"workers"} if leased else set())))
    require(type(raw["steps"]) is list and bool(raw["steps"]) and type(raw["commands"]) is list)
    steps=tuple(parse_step(x) for x in raw["steps"])
    maxa=integer(raw.get("max_attempts",3),1,10); delay=integer(raw.get("retry_delay",2),1,1_000_000)
    if not leased:
        cmds=tuple(parse_old_cmd(x) for x in raw["commands"]); validate_graph(steps); return OldSimulator(steps,cmds,maxa,delay)
    require(len(raw["commands"])<=2000 and type(raw["workers"]) is list and 1<=len(raw["workers"])<=100)
    workers=tuple(ident(x) for x in raw["workers"]);require(len(workers)==len(set(workers)))
    duration=integer(raw.get("lease_duration",5),1,1_000_000)
    cmds=tuple(parse_leased_cmd(x) for x in raw["commands"]); validate_graph(steps)
    return LeasedSimulator(steps,cmds,workers,maxa,delay,duration)


# Main imports these names; parse_workflow now returns the appropriate simulator.
Simulator = object
