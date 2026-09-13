"""Deterministic in-memory DAG runner with durable execution and recovery."""
from dataclasses import dataclass, field
import re
from typing import Any, Literal

TIME_LIMIT = 2_147_483_647
TERMINAL_RUN_STATUSES = ("succeeded", "failed", "cancelled")
COMMAND_LIMIT = 2_000


class DomainError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def require(condition: bool, code: str = "INVALID_INPUT") -> None:
    if not condition:
        raise DomainError(code)


def object_fields(value: Any, required: set[str], optional: set[str] = frozenset()) -> dict:
    require(type(value) is dict)
    require(required <= value.keys() <= required | optional)
    return value


def integer(value: Any, low: int, high: int) -> int:
    require(type(value) is int and low <= value <= high)
    return value


def identifier(value: Any) -> str:
    require(type(value) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) is not None)
    return value


@dataclass(frozen=True)
class StepDefinition:
    id: str
    needs: tuple[str, ...]
    amount: int
    failures: int = 0


@dataclass(frozen=True)
class Command:
    op: str
    run: str | None = None
    by: int = 0
    crash_at: str | None = None


@dataclass(frozen=True)
class Workflow:
    steps: tuple[StepDefinition, ...]
    commands: tuple[Command, ...]
    max_attempts: int
    retry_delay: int


@dataclass(frozen=True)
class LeasedCommand:
    """A checkpoint-two command; leased mode splits claim, call and deliver."""

    op: str
    run: str | None = None
    by: int = 0
    worker: str | None = None
    ticket: int = 0


@dataclass(frozen=True)
class LeasedWorkflow:
    steps: tuple[StepDefinition, ...]
    commands: tuple[LeasedCommand, ...]
    workers: tuple[str, ...]
    max_attempts: int
    retry_delay: int
    lease_duration: int


def parse_step(raw: Any) -> StepDefinition:
    obj = object_fields(raw, {"id", "needs", "amount"}, {"failures"})
    name = identifier(obj["id"])
    require(type(obj["needs"]) is list)
    needs = tuple(identifier(dep) for dep in obj["needs"])
    require(len(needs) == len(set(needs)))
    return StepDefinition(name, needs, integer(obj["amount"], 1, 1_000_000),
                          integer(obj.get("failures", 0), 0, 100))


def parse_command(raw: Any) -> Command:
    require(type(raw) is dict and type(raw.get("op")) is str)
    op = raw["op"]
    if op in ("start", "cancel"):
        object_fields(raw, {"op", "run"})
        return Command(op, run=identifier(raw["run"]))
    if op == "advance":
        object_fields(raw, {"op", "by"})
        return Command(op, by=integer(raw["by"], 0, TIME_LIMIT))
    if op == "tick":
        object_fields(raw, {"op"}, {"crash_at"})
        if "crash_at" in raw:
            require(type(raw["crash_at"]) is str and raw["crash_at"] in ("after_begin", "after_call"))
        return Command(op, crash_at=raw.get("crash_at"))
    require(op in ("observe", "crash", "restart"))
    object_fields(raw, {"op"})
    return Command(op)


def validate_graph(steps: tuple[StepDefinition, ...]) -> None:
    ids = {step.id for step in steps}
    require(len(ids) == len(steps), "DUPLICATE_STEP")
    require(all(dep in ids for step in steps for dep in step.needs), "UNKNOWN_DEPENDENCY")
    # Kahn's algorithm avoids depending on the interpreter's recursion limit.
    remaining = list(steps)
    visited: set[str] = set()
    while remaining:
        ready = [step for step in remaining if set(step.needs) <= visited]
        require(bool(ready), "DEPENDENCY_CYCLE")
        visited.update(step.id for step in ready)
        remaining = [step for step in remaining if step.id not in visited]


def parse_workflow(raw: Any) -> Workflow:
    obj = object_fields(raw, {"steps", "commands"}, {"max_attempts", "retry_delay"})
    require(type(obj["steps"]) is list and bool(obj["steps"]))
    require(type(obj["commands"]) is list)
    steps = tuple(parse_step(step) for step in obj["steps"])
    commands = tuple(parse_command(command) for command in obj["commands"])
    workflow = Workflow(steps, commands, integer(obj.get("max_attempts", 3), 1, 10),
                        integer(obj.get("retry_delay", 2), 1, 1_000_000))
    validate_graph(steps)  # All static validation precedes command execution.
    return workflow


def parse_leased_command(raw: Any) -> LeasedCommand:
    """Parse one leased-mode command; `tick` and its checkpoints are gone."""
    require(type(raw) is dict and type(raw.get("op")) is str)
    op = raw["op"]
    if op in ("start", "cancel"):
        object_fields(raw, {"op", "run"})
        return LeasedCommand(op, run=identifier(raw["run"]))
    if op == "advance":
        object_fields(raw, {"op", "by"})
        return LeasedCommand(op, by=integer(raw["by"], 0, TIME_LIMIT))
    if op in ("crash", "restart", "claim"):
        object_fields(raw, {"op", "worker"})
        return LeasedCommand(op, worker=identifier(raw["worker"]))
    if op in ("renew", "call"):
        object_fields(raw, {"op", "worker", "ticket"})
        return LeasedCommand(op, worker=identifier(raw["worker"]),
                             ticket=integer(raw["ticket"], 1, TIME_LIMIT))
    if op == "deliver":
        object_fields(raw, {"op", "ticket"})
        return LeasedCommand(op, ticket=integer(raw["ticket"], 1, TIME_LIMIT))
    require(op == "observe")
    object_fields(raw, {"op"})
    return LeasedCommand(op)


def parse_leased_workflow(raw: Any) -> LeasedWorkflow:
    obj = object_fields(raw, {"steps", "commands", "workers"},
                        {"max_attempts", "retry_delay", "lease_duration"})
    require(type(obj["steps"]) is list and bool(obj["steps"]))
    require(type(obj["commands"]) is list and len(obj["commands"]) <= COMMAND_LIMIT)
    require(type(obj["workers"]) is list and bool(obj["workers"]))
    steps = tuple(parse_step(step) for step in obj["steps"])
    commands = tuple(parse_leased_command(command) for command in obj["commands"])
    workers = tuple(identifier(worker) for worker in obj["workers"])
    require(len(set(workers)) == len(workers))
    workflow = LeasedWorkflow(steps, commands, workers,
                              integer(obj.get("max_attempts", 3), 1, 10),
                              integer(obj.get("retry_delay", 2), 1, 1_000_000),
                              integer(obj.get("lease_duration", 5), 1, 1_000_000))
    validate_graph(steps)  # Shape and scalar checks still precede graph checks.
    return workflow


@dataclass
class Lease:
    """The durable lease of a running step: who holds it, since when, until when."""

    worker: str
    ticket: int
    expires: int


@dataclass
class StepState:
    id: str
    status: Literal["pending", "running", "succeeded", "failed", "blocked", "cancelled"] = "pending"
    attempts: int = 0
    ready_at: int = 0
    lease: Lease | None = None


@dataclass
class RunState:
    id: str
    steps: list[StepState]
    status: Literal["active", "succeeded", "failed", "failing", "cancelling", "cancelled"] = "active"
    cancel_requested: bool = False


@dataclass(frozen=True)
class ServiceCall:
    key: tuple[str, str]
    attempt: int
    kind: str = "execute"
    outcome: str = "applied"
    worker: str | None = None
    ticket: int | None = None


@dataclass(frozen=True)
class Effect:
    key: tuple[str, str]
    amount: int


@dataclass
class MockService:
    """Idempotent mock external service; its state survives runner crashes."""

    calls: list[ServiceCall] = field(default_factory=list)
    effects: list[Effect] = field(default_factory=list)
    # Effect keys recorded so far, and per-key count of transient failures served.
    applied: set[tuple[str, str]] = field(default_factory=set)
    transients: dict[tuple[str, str], int] = field(default_factory=dict)

    def execute(self, key: tuple[str, str], amount: int, attempt: int, failures: int,
                worker: str | None = None, ticket: int | None = None) -> str:
        """Atomically replay, fail transiently, or record exactly one effect."""
        if key in self.applied:
            outcome = "replayed"
        elif self.transients.get(key, 0) < failures:
            self.transients[key] = self.transients.get(key, 0) + 1
            outcome = "transient"
        else:
            self.applied.add(key)
            self.effects.append(Effect(key, amount))
            outcome = "applied"
        self.calls.append(ServiceCall(key, attempt, "execute", outcome, worker, ticket))
        return outcome

    def lookup(self, key: tuple[str, str], attempt: int,
               worker: str | None = None, ticket: int | None = None) -> str:
        """Report whether this key already has an effect; never records one."""
        outcome = "found" if key in self.applied else "missing"
        self.calls.append(ServiceCall(key, attempt, "lookup", outcome, worker, ticket))
        return outcome


class Simulator:
    def __init__(self, workflow: Workflow):
        self.workflow = workflow
        self.now = 0
        self.up = True
        self.runs: list[RunState] = []
        self.service = MockService()
        self.observations: list[dict] = []

    def select_action(self) -> tuple[RunState, StepState, StepDefinition] | None:
        for run in self.runs:
            for state, definition in zip(run.steps, self.workflow.steps):
                if state.status == "running":
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

    def tick(self, crash_at: str | None = None) -> None:
        action = self.select_action()
        if action is None:
            return  # No eligible work, so no checkpoint is ever reached.
        run, state, definition = action
        key = (run.id, state.id)
        if state.status == "pending":
            # Beginning an attempt is durable and precedes any service call.
            state.status = "running"
            state.attempts += 1
        if crash_at == "after_begin":
            self.up = False
            return
        if run.status == "cancelling":
            outcome = self.service.lookup(key, state.attempts)
            if crash_at == "after_call":
                self.up = False
                return
            state.status = "succeeded" if outcome == "found" else "cancelled"
            run.status = "cancelled"
            return
        outcome = self.service.execute(key, definition.amount, state.attempts, definition.failures)
        if crash_at == "after_call":
            self.up = False
            return
        if outcome == "transient":
            self.commit_transient(run, state)
            return
        state.status = "succeeded"
        if all(step.status == "succeeded" for step in run.steps):
            run.status = "succeeded"

    def commit_transient(self, run: RunState, state: StepState) -> None:
        """Schedule a retry, or fail the run once the attempt budget is spent."""
        if state.attempts < self.workflow.max_attempts:
            deadline = self.now + self.workflow.retry_delay
            require(deadline <= TIME_LIMIT, "TIME_OVERFLOW")
            state.status = "pending"
            state.ready_at = deadline
            return
        state.status = "failed"
        run.status = "failed"
        for step in run.steps:
            if step.status == "pending":
                step.status = "blocked"

    def cancel(self, run_id: str) -> None:
        run = next((candidate for candidate in self.runs if candidate.id == run_id), None)
        require(run is not None, "UNKNOWN_RUN")
        if run.status in TERMINAL_RUN_STATUSES:
            return  # Terminal runs keep their status and cancellation flag.
        run.cancel_requested = True
        for step in run.steps:
            if step.status == "pending":
                step.status = "cancelled"
        running = any(step.status == "running" for step in run.steps)
        run.status = "cancelling" if running else "cancelled"

    def apply(self, command: Command) -> None:
        if command.op == "observe":
            self.observations.append(self.snapshot())
        elif command.op == "advance":
            require(self.now + command.by <= TIME_LIMIT, "TIME_OVERFLOW")
            self.now += command.by
        elif command.op == "restart":
            require(not self.up, "PROCESS_UP")
            self.up = True
        elif command.op == "start":
            require(self.up, "PROCESS_DOWN")
            require(all(run.id != command.run for run in self.runs), "DUPLICATE_RUN")
            self.runs.append(RunState(command.run, [StepState(step.id, ready_at=self.now)
                                                   for step in self.workflow.steps]))
        elif command.op == "tick":
            require(self.up, "PROCESS_DOWN")
            self.tick(command.crash_at)
        elif command.op == "cancel":
            require(self.up, "PROCESS_DOWN")  # Availability precedes run lookup.
            self.cancel(command.run)
        else:  # crash
            require(self.up, "PROCESS_DOWN")
            self.up = False  # Only volatile work is lost; the durable store stands.

    def snapshot(self) -> dict:
        # New containers at every level keep observations independent of mutable state.
        return {"now": self.now, "up": self.up, "runs": [
            {"id": run.id, "status": run.status, "cancel_requested": run.cancel_requested,
             "steps": [{"id": step.id, "status": step.status, "attempts": step.attempts,
                        "ready_at": step.ready_at} for step in run.steps]} for run in self.runs]}

    def run(self) -> dict:
        for command in self.workflow.commands:
            self.apply(command)
        final = self.snapshot()
        final["calls"] = [{"kind": call.kind, "key": list(call.key), "attempt": call.attempt,
                           "outcome": call.outcome} for call in self.service.calls]
        final["effects"] = [{"key": list(effect.key), "amount": effect.amount}
                             for effect in self.service.effects]
        return {"observations": self.observations, "final": final}


@dataclass
class TicketRecord:
    """Metadata of one lease acquisition, kept even after the lease is replaced.

    The saved response models a transport message that a worker crash cannot
    erase: the mock already produced it, so it may still be delivered later.
    """

    id: int
    worker: str
    run: str
    step: str
    kind: Literal["execute", "lookup"]
    attempt: int
    response: str | None = None
    delivered: bool = False


class LeasedSimulator:
    """Checkpoint-two runner: several workers share the graph through leases."""

    def __init__(self, workflow: LeasedWorkflow):
        self.workflow = workflow
        self.now = 0
        self.workers = {worker: True for worker in workflow.workers}
        self.runs: list[RunState] = []
        self.service = MockService()
        self.tickets: dict[int, TicketRecord] = {}
        self.next_ticket = 1
        self.results: list[Any] = []

    # -- lookups -----------------------------------------------------------

    def live(self, lease: Lease | None) -> bool:
        """A lease is live exactly while now < expires; equality is expired."""
        return lease is not None and self.now < lease.expires

    def find_run(self, run_id: str) -> RunState:
        run = next((candidate for candidate in self.runs if candidate.id == run_id), None)
        require(run is not None, "UNKNOWN_RUN")
        return run

    def require_worker(self, worker: str, must_be_up: bool = True) -> None:
        require(worker in self.workers, "UNKNOWN_WORKER")
        if must_be_up:
            require(self.workers[worker], "WORKER_DOWN")

    def require_ticket(self, ticket: int, worker: str | None = None) -> TicketRecord:
        record = self.tickets.get(ticket)
        require(record is not None, "UNKNOWN_TICKET")
        if worker is not None:
            require(record.worker == worker, "WRONG_WORKER")
        return record

    def locate(self, record: TicketRecord) -> tuple[RunState, StepState, StepDefinition]:
        run = next(candidate for candidate in self.runs if candidate.id == record.run)
        for state, definition in zip(run.steps, self.workflow.steps):
            if state.id == record.step:
                return run, state, definition
        raise DomainError("UNKNOWN_TICKET")  # Unreachable: tickets name real steps.

    def current(self, record: TicketRecord, state: StepState) -> bool:
        """True when this acquisition still owns the step's durable lease."""
        return state.lease is not None and state.lease.ticket == record.id

    # -- commands ----------------------------------------------------------

    def start(self, run_id: str) -> None:
        require(all(run.id != run_id for run in self.runs), "DUPLICATE_RUN")
        self.runs.append(RunState(run_id, [StepState(step.id, ready_at=self.now)
                                           for step in self.workflow.steps]))

    def select_action(self) -> tuple[RunState, StepState, StepDefinition] | None:
        """Recover abandoned running work first, then start ready pending work."""
        for run in self.runs:
            if run.status in TERMINAL_RUN_STATUSES:
                continue
            for state, definition in zip(run.steps, self.workflow.steps):
                if state.status == "running" and not self.live(state.lease):
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
        busy = any(self.live(step.lease) and step.lease.worker == worker
                   for run in self.runs for step in run.steps)
        require(not busy, "WORKER_BUSY")
        action = self.select_action()
        if action is None:
            return {"ticket": None}  # No eligible work consumes no ticket ID.
        run, state, definition = action
        expires = self.now + self.workflow.lease_duration
        require(expires <= TIME_LIMIT, "TIME_OVERFLOW")
        if state.status == "pending":
            # Only a fresh attempt increments the durable attempt number.
            state.status = "running"
            state.attempts += 1
        ticket = self.next_ticket
        self.next_ticket += 1
        kind = "lookup" if run.status in ("cancelling", "failing") else "execute"
        state.lease = Lease(worker, ticket, expires)
        self.tickets[ticket] = TicketRecord(ticket, worker, run.id, state.id, kind, state.attempts)
        return {"ticket": ticket}

    def renew(self, worker: str, ticket: int) -> dict:
        self.require_worker(worker)
        record = self.require_ticket(ticket, worker)
        _, state, _ = self.locate(record)
        if not (self.current(record, state) and self.live(state.lease)):
            return {"renewed": False}
        expires = self.now + self.workflow.lease_duration
        require(expires <= TIME_LIMIT, "TIME_OVERFLOW")
        state.lease.expires = expires  # Same ticket, attempt and logical key.
        return {"renewed": True}

    def call(self, worker: str, ticket: int) -> dict:
        self.require_worker(worker)
        record = self.require_ticket(ticket, worker)
        run, state, definition = self.locate(record)
        if not (self.current(record, state) and self.live(state.lease)):
            return {"outcome": "stale"}  # A fenced worker performs no service call.
        if record.response is None:
            key = (record.run, record.step)
            if record.kind == "execute":
                record.response = self.service.execute(key, definition.amount, record.attempt,
                                                       definition.failures, worker, ticket)
            else:
                record.response = self.service.lookup(key, record.attempt, worker, ticket)
        return {"kind": record.kind, "outcome": record.response}

    def deliver(self, ticket: int) -> dict:
        record = self.require_ticket(ticket)
        if record.response is None or record.delivered:
            return {"committed": False}
        run, state, definition = self.locate(record)
        if not (self.current(record, state) and self.live(state.lease)):
            return {"committed": False}  # A stale response never overwrites state.
        if not self.workers[record.worker]:
            return {"committed": False}
        record.delivered = True
        state.lease = None
        self.commit(run, state, record)
        return {"committed": True}

    def commit(self, run: RunState, state: StepState, record: TicketRecord) -> None:
        if record.kind == "lookup":
            # Reconciliation never starts work; it only reads the effect set.
            if record.response == "found":
                state.status = "succeeded"
            else:
                state.status = "cancelled" if run.status == "cancelling" else "blocked"
        elif record.response == "transient":
            self.commit_transient(run, state)
            return
        else:
            state.status = "succeeded"
        self.settle(run)

    def commit_transient(self, run: RunState, state: StepState) -> None:
        """Retry from delivery time, or fail the step and drain the run."""
        if state.attempts < self.workflow.max_attempts:
            deadline = self.now + self.workflow.retry_delay
            require(deadline <= TIME_LIMIT, "TIME_OVERFLOW")
            state.status = "pending"
            state.ready_at = deadline
            return
        state.status = "failed"
        for step in run.steps:
            if step.status == "pending":
                step.status = "blocked"
        if any(step.status == "running" for step in run.steps):
            run.status = "failing"
            self.expire_leases(run)  # Other branches finish through lookup only.
        else:
            run.status = "failed"

    def expire_leases(self, run: RunState) -> None:
        for step in run.steps:
            if step.status == "running" and step.lease is not None:
                step.lease.expires = self.now  # Ticket and worker stay visible.

    def settle(self, run: RunState) -> None:
        """Promote a draining or finished run once no running step remains."""
        if run.status == "active":
            if all(step.status == "succeeded" for step in run.steps):
                run.status = "succeeded"
        elif run.status in ("cancelling", "failing"):
            if not any(step.status == "running" for step in run.steps):
                run.status = "cancelled" if run.status == "cancelling" else "failed"

    def cancel(self, run_id: str) -> None:
        run = self.find_run(run_id)
        if run.status in TERMINAL_RUN_STATUSES or run.status == "failing":
            return
        if run.cancel_requested:
            return  # Repeated cancellation must not revoke new lookup leases.
        run.cancel_requested = True
        for step in run.steps:
            if step.status == "pending":
                step.status = "cancelled"
        if any(step.status == "running" for step in run.steps):
            run.status = "cancelling"
            self.expire_leases(run)  # Any in-flight execute response is now stale.
        else:
            run.status = "cancelled"

    def apply(self, command: LeasedCommand) -> Any:
        if command.op == "observe":
            return self.snapshot()
        if command.op == "advance":
            require(self.now + command.by <= TIME_LIMIT, "TIME_OVERFLOW")
            self.now += command.by
            return None
        if command.op == "start":
            self.start(command.run)
            return None
        if command.op == "cancel":
            self.cancel(command.run)
            return None
        if command.op == "crash":
            self.require_worker(command.worker)  # Already down is WORKER_DOWN.
            self.workers[command.worker] = False
            return None
        if command.op == "restart":
            self.require_worker(command.worker, must_be_up=False)
            require(not self.workers[command.worker], "WORKER_UP")
            self.workers[command.worker] = True
            return None
        if command.op == "claim":
            return self.claim(command.worker)
        if command.op == "renew":
            return self.renew(command.worker, command.ticket)
        if command.op == "call":
            return self.call(command.worker, command.ticket)
        return self.deliver(command.ticket)

    def snapshot(self) -> dict:
        # Fresh containers at every level keep earlier values immutable.
        return {"now": self.now,
                "workers": [{"id": worker, "up": up} for worker, up in self.workers.items()],
                "runs": [
                    {"id": run.id, "status": run.status, "cancel_requested": run.cancel_requested,
                     "steps": [{"id": step.id, "status": step.status, "attempts": step.attempts,
                                "ready_at": step.ready_at,
                                "lease": None if step.lease is None else
                                {"worker": step.lease.worker, "ticket": step.lease.ticket,
                                 "expires": step.lease.expires}}
                               for step in run.steps]} for run in self.runs]}

    def run(self) -> dict:
        for command in self.workflow.commands:
            self.results.append(self.apply(command))
        final = self.snapshot()
        final["calls"] = [{"worker": call.worker, "ticket": call.ticket, "kind": call.kind,
                           "key": list(call.key), "attempt": call.attempt, "outcome": call.outcome}
                          for call in self.service.calls]
        final["effects"] = [{"key": list(effect.key), "amount": effect.amount}
                            for effect in self.service.effects]
        return {"results": self.results, "final": final}


def build(raw: Any) -> Simulator | LeasedSimulator:
    """Select the previous contract or leased mode from the `workers` field."""
    require(type(raw) is dict)
    if "workers" in raw:
        return LeasedSimulator(parse_leased_workflow(raw))
    return Simulator(parse_workflow(raw))
