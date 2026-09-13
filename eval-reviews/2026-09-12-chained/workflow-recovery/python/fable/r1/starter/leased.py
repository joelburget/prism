"""Checkpoint two: leased workers, delayed calls/commits, fencing, and failure draining.

This module implements the `workers` input mode. The checkpoint-one simulator in
`workflow.py` is untouched; the shared validation helpers, step parsing, graph checks,
and the idempotent mock service are reused from it.
"""
from dataclasses import dataclass, field
from typing import Any, Literal

from workflow import (TIME_LIMIT, DomainError, MockService, StepDefinition, identifier,
                      integer, object_fields, parse_step, require, validate_graph)

COMMAND_LIMIT = 2_000


@dataclass(frozen=True)
class LeasedCommand:
    op: str
    run: str | None = None
    by: int = 0
    worker: str | None = None
    ticket: int | None = None


@dataclass(frozen=True)
class LeasedWorkflow:
    steps: tuple[StepDefinition, ...]
    commands: tuple[LeasedCommand, ...]
    workers: tuple[str, ...]
    max_attempts: int
    retry_delay: int
    lease_duration: int


def parse_leased_command(raw: Any) -> LeasedCommand:
    require(type(raw) is dict and type(raw.get("op")) is str)
    op = raw["op"]
    if op in ("start", "cancel"):
        object_fields(raw, {"op", "run"})
        return LeasedCommand(op, run=identifier(raw["run"]))
    if op == "advance":
        object_fields(raw, {"op", "by"})
        return LeasedCommand(op, by=integer(raw["by"], 0, TIME_LIMIT))
    if op == "observe":
        object_fields(raw, {"op"})
        return LeasedCommand(op)
    if op in ("crash", "restart", "claim"):
        object_fields(raw, {"op", "worker"})
        return LeasedCommand(op, worker=identifier(raw["worker"]))
    if op in ("renew", "call"):
        object_fields(raw, {"op", "worker", "ticket"})
        return LeasedCommand(op, worker=identifier(raw["worker"]),
                             ticket=integer(raw["ticket"], 1, TIME_LIMIT))
    require(op == "deliver")
    object_fields(raw, {"op", "ticket"})
    return LeasedCommand(op, ticket=integer(raw["ticket"], 1, TIME_LIMIT))


def parse_leased_workflow(raw: Any) -> LeasedWorkflow:
    obj = object_fields(raw, {"steps", "commands", "workers"},
                        {"max_attempts", "retry_delay", "lease_duration"})
    require(type(obj["steps"]) is list and bool(obj["steps"]))
    require(type(obj["commands"]) is list and len(obj["commands"]) <= COMMAND_LIMIT)
    require(type(obj["workers"]) is list and bool(obj["workers"]))
    steps = tuple(parse_step(step) for step in obj["steps"])
    workers = tuple(identifier(worker) for worker in obj["workers"])
    require(len(workers) == len(set(workers)))
    commands = tuple(parse_leased_command(command) for command in obj["commands"])
    workflow = LeasedWorkflow(steps, commands, workers,
                              integer(obj.get("max_attempts", 3), 1, 10),
                              integer(obj.get("retry_delay", 2), 1, 1_000_000),
                              integer(obj.get("lease_duration", 5), 1, 1_000_000))
    validate_graph(steps)  # Field/scalar validation completes before graph checks.
    return workflow


@dataclass
class Lease:
    worker: str
    ticket: int
    expires: int


@dataclass
class LeasedStep:
    id: str
    status: Literal["pending", "running", "succeeded", "failed", "blocked", "cancelled"] = "pending"
    attempts: int = 0
    ready_at: int = 0
    lease: Lease | None = None


@dataclass
class LeasedRun:
    id: str
    steps: list[LeasedStep]
    status: Literal["active", "succeeded", "failed", "cancelling", "cancelled", "failing"] = "active"
    cancel_requested: bool = False

    @property
    def terminal(self) -> bool:
        return self.status in ("succeeded", "failed", "cancelled")

    def has_running(self) -> bool:
        return any(step.status == "running" for step in self.steps)


@dataclass
class Ticket:
    """Durable acquisition metadata plus the delayed transport message (saved response)."""
    worker: str
    run: LeasedRun
    step: LeasedStep
    definition: StepDefinition
    kind: Literal["execute", "lookup"]
    attempt: int
    response: str | None = None
    delivered: bool = False


@dataclass
class AuditedService(MockService):
    """The checkpoint-one mock plus worker/ticket attribution for each audited call."""
    origins: list[tuple[str, int]] = field(default_factory=list)

    def execute_as(self, worker: str, ticket: int, key: tuple[str, str], amount: int,
                   attempt: int) -> str:
        self.origins.append((worker, ticket))
        return self.execute(key, amount, attempt)

    def lookup_as(self, worker: str, ticket: int, key: tuple[str, str], attempt: int) -> str:
        self.origins.append((worker, ticket))
        return self.lookup(key, attempt)


class LeasedSimulator:
    def __init__(self, workflow: LeasedWorkflow):
        self.workflow = workflow
        self.now = 0
        self.workers: dict[str, bool] = {worker: True for worker in workflow.workers}
        self.runs: list[LeasedRun] = []
        self.tickets: dict[int, Ticket] = {}
        self.next_ticket = 1
        self.service = AuditedService({step.id: step.failures for step in workflow.steps})
        self.results: list[Any] = []

    # ----- lookups and validation -------------------------------------------------
    def find_run(self, run_id: str) -> LeasedRun:
        for run in self.runs:
            if run.id == run_id:
                return run
        raise DomainError("UNKNOWN_RUN")

    def require_worker(self, worker: str, up: bool = True) -> None:
        require(worker in self.workers, "UNKNOWN_WORKER")
        if up:
            require(self.workers[worker], "WORKER_DOWN")

    def require_ticket(self, worker: str, ticket: int) -> Ticket:
        require(ticket in self.tickets, "UNKNOWN_TICKET")
        record = self.tickets[ticket]
        require(record.worker == worker, "WRONG_WORKER")
        return record

    def is_live(self, lease: Lease | None) -> bool:
        return lease is not None and self.now < lease.expires

    def is_current_live(self, ticket: int, record: Ticket) -> bool:
        lease = record.step.lease
        return lease is not None and lease.ticket == ticket and self.is_live(lease)

    def lease_deadline(self) -> int:
        deadline = self.now + self.workflow.lease_duration
        require(deadline <= TIME_LIMIT, "TIME_OVERFLOW")
        return deadline

    # ----- scheduling --------------------------------------------------------------
    def select_work(self) -> tuple[LeasedRun, LeasedStep, StepDefinition] | None:
        for run in self.runs:
            for state, definition in zip(run.steps, self.workflow.steps):
                if state.status == "running" and not self.is_live(state.lease):
                    return run, state, definition
        for run in self.runs:
            if run.status != "active":
                continue
            succeeded = {step.id for step in run.steps if step.status == "succeeded"}
            for state, definition in zip(run.steps, self.workflow.steps):
                if (state.status == "pending" and state.ready_at <= self.now
                        and all(dep in succeeded for dep in definition.needs)):
                    return run, state, definition
        return None

    def claim(self, worker: str) -> dict:
        self.require_worker(worker)
        for run in self.runs:
            for step in run.steps:
                lease = step.lease
                require(not (lease is not None and lease.worker == worker
                             and self.is_live(lease)), "WORKER_BUSY")
        work = self.select_work()
        if work is None:
            return {"ticket": None}
        run, state, definition = work
        expires = self.lease_deadline()
        ticket = self.next_ticket
        self.next_ticket += 1
        if state.status == "pending":
            state.status = "running"
            state.attempts += 1
        kind = "execute" if run.status == "active" else "lookup"
        state.lease = Lease(worker, ticket, expires)
        self.tickets[ticket] = Ticket(worker, run, state, definition, kind, state.attempts)
        return {"ticket": ticket}

    def renew(self, worker: str, ticket: int) -> dict:
        self.require_worker(worker)
        record = self.require_ticket(worker, ticket)
        if not self.is_current_live(ticket, record):
            return {"renewed": False}
        record.step.lease.expires = self.lease_deadline()
        return {"renewed": True}

    def call(self, worker: str, ticket: int) -> dict:
        self.require_worker(worker)
        record = self.require_ticket(worker, ticket)
        if not self.is_current_live(ticket, record):
            return {"outcome": "stale"}
        if record.response is None:
            key = (record.run.id, record.step.id)
            if record.kind == "execute":
                record.response = self.service.execute_as(worker, ticket, key,
                                                          record.definition.amount, record.attempt)
            else:
                record.response = self.service.lookup_as(worker, ticket, key, record.attempt)
        return {"kind": record.kind, "outcome": record.response}

    # ----- commit -----------------------------------------------------------------
    def deliver(self, ticket: int) -> dict:
        require(ticket in self.tickets, "UNKNOWN_TICKET")
        record = self.tickets[ticket]
        if (record.response is None or record.delivered or not self.workers[record.worker]
                or not self.is_current_live(ticket, record)):
            return {"committed": False}
        record.delivered = True
        run, step = record.run, record.step
        step.lease = None
        if record.kind == "lookup":
            if record.response == "found":
                step.status = "succeeded"
            else:
                step.status = "cancelled" if run.status == "cancelling" else "blocked"
            if not run.has_running():
                run.status = "cancelled" if run.status == "cancelling" else "failed"
        elif record.response in ("applied", "replayed"):
            step.status = "succeeded"
            if all(s.status == "succeeded" for s in run.steps):
                run.status = "succeeded"
        elif step.attempts < self.workflow.max_attempts:
            deadline = self.now + self.workflow.retry_delay
            require(deadline <= TIME_LIMIT, "TIME_OVERFLOW")
            step.status = "pending"
            step.ready_at = deadline
        else:
            step.status = "failed"
            for other in run.steps:
                if other.status == "pending":
                    other.status = "blocked"
            if run.has_running():
                run.status = "failing"
                self.expire_running(run)
            else:
                run.status = "failed"
        return {"committed": True}

    def expire_running(self, run: LeasedRun) -> None:
        for step in run.steps:
            if step.status == "running" and step.lease is not None:
                step.lease.expires = self.now

    def cancel(self, run: LeasedRun) -> None:
        if run.status != "active":
            return  # Terminal, failing, and repeated cancellations are no-ops.
        run.cancel_requested = True
        for step in run.steps:
            if step.status == "pending":
                step.status = "cancelled"
        if run.has_running():
            run.status = "cancelling"
            self.expire_running(run)
        else:
            run.status = "cancelled"

    # ----- command dispatch -------------------------------------------------------
    def apply(self, command: LeasedCommand) -> Any:
        op = command.op
        if op == "start":
            require(all(run.id != command.run for run in self.runs), "DUPLICATE_RUN")
            self.runs.append(LeasedRun(command.run, [LeasedStep(step.id, ready_at=self.now)
                                                     for step in self.workflow.steps]))
            return None
        if op == "advance":
            require(self.now + command.by <= TIME_LIMIT, "TIME_OVERFLOW")
            self.now += command.by
            return None
        if op == "observe":
            return self.snapshot()
        if op == "crash":
            self.require_worker(command.worker)
            self.workers[command.worker] = False
            return None
        if op == "restart":
            self.require_worker(command.worker, up=False)
            require(not self.workers[command.worker], "WORKER_UP")
            self.workers[command.worker] = True
            return None
        if op == "claim":
            return self.claim(command.worker)
        if op == "renew":
            return self.renew(command.worker, command.ticket)
        if op == "call":
            return self.call(command.worker, command.ticket)
        if op == "deliver":
            return self.deliver(command.ticket)
        if op == "cancel":
            self.cancel(self.find_run(command.run))
            return None
        raise DomainError("INVALID_INPUT")

    def snapshot(self) -> dict:
        # Fresh containers at every level keep earlier results immutable.
        return {
            "now": self.now,
            "workers": [{"id": worker, "up": up} for worker, up in self.workers.items()],
            "runs": [{"id": run.id, "status": run.status, "cancel_requested": run.cancel_requested,
                      "steps": [{"id": step.id, "status": step.status, "attempts": step.attempts,
                                 "ready_at": step.ready_at,
                                 "lease": None if step.lease is None else {
                                     "worker": step.lease.worker, "ticket": step.lease.ticket,
                                     "expires": step.lease.expires}}
                                for step in run.steps]} for run in self.runs],
        }

    def run(self) -> dict:
        for command in self.workflow.commands:
            self.results.append(self.apply(command))
        final = self.snapshot()
        final["calls"] = [{"worker": worker, "ticket": ticket, "kind": call.kind,
                           "key": list(call.key), "attempt": call.attempt, "outcome": call.outcome}
                          for (worker, ticket), call in zip(self.service.origins, self.service.calls)]
        final["effects"] = [{"key": list(effect.key), "amount": effect.amount}
                            for effect in self.service.effects]
        return {"results": self.results, "final": final}
