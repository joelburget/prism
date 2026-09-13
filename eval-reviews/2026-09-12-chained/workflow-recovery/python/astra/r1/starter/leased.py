"""Leased execution with durable acquisitions and separately delivered responses."""
from dataclasses import dataclass

from workflow import (
    TIME_LIMIT, RunState, Simulator, StepState, Workflow, identifier, integer,
    object_fields, parse_command, parse_step, require, validate_graph,
)


def parse_leased(raw):
    obj = object_fields(raw, {"steps", "commands", "workers"},
                        {"max_attempts", "retry_delay", "lease_duration"})
    require(type(obj["workers"]) is list and bool(obj["workers"]))
    workers = tuple(identifier(worker) for worker in obj["workers"])
    require(len(workers) == len(set(workers)))
    require(type(obj["steps"]) is list and bool(obj["steps"]))
    require(type(obj["commands"]) is list and len(obj["commands"]) <= 2000)
    steps = tuple(parse_step(step) for step in obj["steps"])
    for cmd in obj["commands"]:
        require(type(cmd) is dict and type(cmd.get("op")) is str)
        op = cmd["op"]
        if op in ("start", "cancel", "advance", "observe"):
            parse_command(cmd)
        elif op in ("crash", "restart", "claim", "renew", "call"):
            fields = {"op", "worker"}
            if op in ("renew", "call"):
                fields.add("ticket")
            object_fields(cmd, fields)
            identifier(cmd["worker"])
            if "ticket" in cmd:
                integer(cmd["ticket"], 1, TIME_LIMIT)
        else:
            require(op == "deliver")
            object_fields(cmd, {"op", "ticket"})
            integer(cmd["ticket"], 1, TIME_LIMIT)
    workflow = Workflow(steps, (), integer(obj.get("max_attempts", 3), 1, 10),
                        integer(obj.get("retry_delay", 2), 1, 1_000_000))
    duration = integer(obj.get("lease_duration", 5), 1, 1_000_000)
    validate_graph(steps)
    return workflow, workers, duration, obj["commands"]


@dataclass
class LeasedStep(StepState):
    lease: dict | None = None


@dataclass
class Ticket:
    worker: str
    run: RunState
    step: LeasedStep
    definition: object
    kind: str
    receipt: dict | None = None
    delivered: bool = False


class LeasedSimulator(Simulator):
    def __init__(self, raw):
        workflow, workers, self.duration, self.commands = parse_leased(raw)
        super().__init__(workflow)
        self.workers = dict.fromkeys(workers, True)
        self.tickets = {}
        self.audit = []

    def live(self, number, ticket):
        lease = ticket.step.lease
        return (lease is not None and lease["ticket"] == number
                and self.now < lease["expires"])

    def deadline(self, delay):
        result = self.now + delay
        require(result <= TIME_LIMIT, "TIME_OVERFLOW")
        return result

    def claim(self, worker):
        require(not any(step.lease is not None and step.lease["worker"] == worker
                        and self.now < step.lease["expires"]
                        for run in self.runs for step in run.steps), "WORKER_BUSY")
        action = None
        for run in self.runs:
            for step, definition in zip(run.steps, self.workflow.steps):
                if step.status == "running" and step.lease["expires"] <= self.now:
                    action = run, step, definition
                    break
            if action is not None:
                break
        if action is None:
            for run in self.runs:
                if run.status != "active":
                    continue
                succeeded = {step.id for step in run.steps if step.status == "succeeded"}
                for step, definition in zip(run.steps, self.workflow.steps):
                    if (step.status == "pending" and step.ready_at <= self.now
                            and all(dep in succeeded for dep in definition.needs)):
                        action = run, step, definition
                        break
                if action is not None:
                    break
        if action is None:
            return {"ticket": None}
        expires = self.deadline(self.duration)
        run, step, definition = action
        number = len(self.tickets) + 1
        if step.status == "pending":
            step.status = "running"
            step.attempts += 1
        step.lease = {"worker": worker, "ticket": number, "expires": expires}
        self.tickets[number] = Ticket(worker, run, step, definition,
                                      "execute" if run.status == "active" else "lookup")
        return {"ticket": number}

    def call(self, number, ticket):
        if not self.live(number, ticket):
            return {"outcome": "stale"}
        if ticket.receipt is None:
            key = (ticket.run.id, ticket.step.id)
            attempt = ticket.step.attempts
            if ticket.kind == "lookup":
                outcome = self.service.lookup(key, attempt)
            else:
                outcome = self.service.execute(key, ticket.definition.amount, attempt,
                                               ticket.definition.failures)
            ticket.receipt = {"kind": ticket.kind, "outcome": outcome}
            self.audit.append({"worker": ticket.worker, "ticket": number,
                               "kind": ticket.kind, "key": list(key),
                               "attempt": attempt, "outcome": outcome})
        return dict(ticket.receipt)

    def expire_running(self, run):
        for step in run.steps:
            if step.status == "running":
                step.lease["expires"] = self.now

    def deliver(self, number, ticket):
        if (ticket.receipt is None or ticket.delivered or not self.live(number, ticket)
                or not self.workers[ticket.worker]):
            return {"committed": False}
        run, step = ticket.run, ticket.step
        outcome = ticket.receipt["outcome"]
        if ticket.kind == "lookup":
            step.status = ("succeeded" if outcome == "found" else
                           "cancelled" if run.status == "cancelling" else "blocked")
        elif outcome == "transient":
            if step.attempts < self.workflow.max_attempts:
                step.ready_at = self.deadline(self.workflow.retry_delay)
                step.status = "pending"
            else:
                step.status = "failed"
                for other in run.steps:
                    if other.status == "pending":
                        other.status = "blocked"
                run.status = "failing"
                self.expire_running(run)
        else:
            step.status = "succeeded"
        step.lease = None
        ticket.delivered = True
        if not any(other.status == "running" for other in run.steps):
            if run.status == "cancelling":
                run.status = "cancelled"
            elif run.status == "failing":
                run.status = "failed"
        if run.status == "active" and all(other.status == "succeeded" for other in run.steps):
            run.status = "succeeded"
        return {"committed": True}

    def cancel(self, run_id):
        run = next((run for run in self.runs if run.id == run_id), None)
        require(run is not None, "UNKNOWN_RUN")
        if run.status != "active":
            return
        run.cancel_requested = True
        for step in run.steps:
            if step.status == "pending":
                step.status = "cancelled"
        self.expire_running(run)
        run.status = ("cancelling" if any(step.status == "running" for step in run.steps)
                      else "cancelled")

    def apply(self, cmd):
        op = cmd["op"]
        if "worker" in cmd:
            worker = cmd["worker"]
            require(worker in self.workers, "UNKNOWN_WORKER")
            if op == "restart":
                require(not self.workers[worker], "WORKER_UP")
            else:
                require(self.workers[worker], "WORKER_DOWN")
        if "ticket" in cmd:
            number = cmd["ticket"]
            require(number in self.tickets, "UNKNOWN_TICKET")
            ticket = self.tickets[number]
            if "worker" in cmd:
                require(ticket.worker == worker, "WRONG_WORKER")
        if op == "start":
            require(all(run.id != cmd["run"] for run in self.runs), "DUPLICATE_RUN")
            self.runs.append(RunState(cmd["run"], [LeasedStep(step.id, ready_at=self.now)
                                                  for step in self.workflow.steps]))
        elif op == "advance":
            self.now = self.deadline(cmd["by"])
        elif op == "observe":
            return self.snapshot()
        elif op in ("crash", "restart"):
            self.workers[worker] = op == "restart"
        elif op == "claim":
            return self.claim(worker)
        elif op == "renew":
            live = self.live(number, ticket)
            if live:
                ticket.step.lease["expires"] = self.deadline(self.duration)
            return {"renewed": live}
        elif op == "call":
            return self.call(number, ticket)
        elif op == "deliver":
            return self.deliver(number, ticket)
        elif op == "cancel":
            self.cancel(cmd["run"])
        return None

    def snapshot(self):
        snapshot = super().snapshot()
        del snapshot["up"]
        snapshot["workers"] = [{"id": worker, "up": up} for worker, up in self.workers.items()]
        for saved, run in zip(snapshot["runs"], self.runs):
            for value, step in zip(saved["steps"], run.steps):
                value["lease"] = dict(step.lease) if step.lease is not None else None
        return snapshot

    def run(self):
        results = [self.apply(cmd) for cmd in self.commands]
        final = self.snapshot()
        final["calls"] = self.audit
        final["effects"] = [{"key": list(effect.key), "amount": effect.amount}
                            for effect in self.service.effects]
        return {"results": results, "final": final}
