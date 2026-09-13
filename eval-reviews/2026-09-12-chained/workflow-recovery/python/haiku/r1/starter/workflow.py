"""Deterministic in-memory DAG runner with durable persistence and recovery."""
from dataclasses import dataclass, field
import re
from typing import Any, Literal

TIME_LIMIT = 2_147_483_647


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
    worker: str | None = None
    ticket: int | None = None


@dataclass(frozen=True)
class Workflow:
    steps: tuple[StepDefinition, ...]
    commands: tuple[Command, ...]
    max_attempts: int
    retry_delay: int
    workers: tuple[str, ...] | None = None
    lease_duration: int = 5


def parse_step(raw: Any) -> StepDefinition:
    obj = object_fields(raw, {"id", "needs", "amount"}, {"failures"})
    name = identifier(obj["id"])
    require(type(obj["needs"]) is list)
    needs = tuple(identifier(dep) for dep in obj["needs"])
    require(len(needs) == len(set(needs)))
    return StepDefinition(name, needs, integer(obj["amount"], 1, 1_000_000),
                          integer(obj.get("failures", 0), 0, 100))


def parse_command_v1(raw: Any) -> Command:
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


def parse_command_v2(raw: Any) -> Command:
    require(type(raw) is dict and type(raw.get("op")) is str)
    op = raw["op"]
    if op == "start":
        object_fields(raw, {"op", "run"})
        return Command(op, run=identifier(raw["run"]))
    if op == "advance":
        object_fields(raw, {"op", "by"})
        return Command(op, by=integer(raw["by"], 0, TIME_LIMIT))
    if op == "observe":
        object_fields(raw, {"op"})
        return Command(op)
    if op in ("crash", "restart"):
        object_fields(raw, {"op", "worker"})
        return Command(op, worker=identifier(raw["worker"]))
    if op == "cancel":
        object_fields(raw, {"op", "run"})
        return Command(op, run=identifier(raw["run"]))
    if op == "claim":
        object_fields(raw, {"op", "worker"})
        return Command(op, worker=identifier(raw["worker"]))
    if op == "renew":
        object_fields(raw, {"op", "worker", "ticket"})
        return Command(op, worker=identifier(raw["worker"]), ticket=integer(raw["ticket"], 1, TIME_LIMIT))
    if op == "call":
        object_fields(raw, {"op", "worker", "ticket"})
        return Command(op, worker=identifier(raw["worker"]), ticket=integer(raw["ticket"], 1, TIME_LIMIT))
    if op == "deliver":
        object_fields(raw, {"op", "ticket"})
        return Command(op, ticket=integer(raw["ticket"], 1, TIME_LIMIT))
    require(False)


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
    # Determine if this is v1 or v2 format
    obj = object_fields(raw, {"steps", "commands"}, {"max_attempts", "retry_delay", "workers", "lease_duration"})
    require(type(obj["steps"]) is list and bool(obj["steps"]))
    require(type(obj["commands"]) is list)
    require(len(obj["commands"]) <= 2000)
    
    steps = tuple(parse_step(step) for step in obj["steps"])
    require(len(steps) <= 200)
    
    # Check if this is v2 (leased worker mode)
    has_workers = "workers" in obj
    
    if has_workers:
        require(type(obj["workers"]) is list and bool(obj["workers"]))
        require(len(obj["workers"]) <= 100)
        workers = tuple(identifier(w) for w in obj["workers"])
        require(len(workers) == len(set(workers)))
        commands = tuple(parse_command_v2(cmd) for cmd in obj["commands"])
        lease_duration = integer(obj.get("lease_duration", 5), 1, 1_000_000)
    else:
        workers = None
        commands = tuple(parse_command_v1(cmd) for cmd in obj["commands"])
        lease_duration = 5
    
    workflow = Workflow(steps, commands, integer(obj.get("max_attempts", 3), 1, 10),
                        integer(obj.get("retry_delay", 2), 1, 1_000_000),
                        workers, lease_duration)
    validate_graph(steps)  # All static validation precedes command execution.
    return workflow


@dataclass
class StepState:
    id: str
    status: Literal["pending", "running", "succeeded", "failed", "blocked", "cancelled"] = "pending"
    attempts: int = 0
    ready_at: int = 0
    lease: 'Lease | None' = None


@dataclass(frozen=True)
class Lease:
    worker: str
    ticket: int
    expires: int


@dataclass
class RunState:
    id: str
    steps: list[StepState]
    status: Literal["active", "succeeded", "failed", "cancelling", "cancelled", "failing"] = "active"
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
    calls: list[ServiceCall] = field(default_factory=list)
    effects: list[Effect] = field(default_factory=list)
    effect_map: dict[tuple[str, str], Effect] = field(default_factory=dict)
    failure_counters: dict[tuple[str, str], int] = field(default_factory=dict)

    def execute(self, key: tuple[str, str], amount: int, attempt: int, failures_config: int, 
                worker: str | None = None, ticket: int | None = None) -> str:
        if key in self.effect_map:
            self.calls.append(ServiceCall(key, attempt, outcome="replayed", worker=worker, ticket=ticket))
            return "replayed"
        
        counter = self.failure_counters.get(key, 0)
        if counter < failures_config:
            self.failure_counters[key] = counter + 1
            self.calls.append(ServiceCall(key, attempt, outcome="transient", worker=worker, ticket=ticket))
            return "transient"
        
        self.calls.append(ServiceCall(key, attempt, worker=worker, ticket=ticket))
        self.effects.append(Effect(key, amount))
        self.effect_map[key] = Effect(key, amount)
        return "applied"
    
    def lookup(self, key: tuple[str, str], attempt: int, worker: str | None = None, 
               ticket: int | None = None) -> str:
        if key in self.effect_map:
            self.calls.append(ServiceCall(key, attempt, kind="lookup", outcome="found", worker=worker, ticket=ticket))
            return "found"
        else:
            self.calls.append(ServiceCall(key, attempt, kind="lookup", outcome="missing", worker=worker, ticket=ticket))
            return "missing"


@dataclass
class WorkerState:
    id: str
    up: bool = True


@dataclass
class TicketInfo:
    worker: str
    step_key: tuple[str, str]
    kind: str
    attempt: int
    response: str | None = None
    response_delivered: bool = False


class Simulator:
    def __init__(self, workflow: Workflow):
        self.workflow = workflow
        self.now = 0
        self.up = True
        self.runs: list[RunState] = []
        self.service = MockService()
        self.observations: list[dict] = []
        self.v2 = workflow.workers is not None
        
        # V2-only state
        if self.v2:
            self.workers = {w: WorkerState(w) for w in workflow.workers}
            self.ticket_counter = 0
            self.tickets: dict[int, TicketInfo] = {}
        
        self.results: list[Any] = []

    def select_action(self) -> tuple[RunState, StepState, StepDefinition] | None:
        # First priority: in-flight steps (already running)
        for run in self.runs:
            if run.status not in ("active", "cancelling"):
                continue
            for state, definition in zip(run.steps, self.workflow.steps):
                if state.status == "running":
                    return run, state, definition
        
        # Second priority: ready pending steps in runs that are not terminal
        for run in self.runs:
            if run.status not in ("active", "cancelling"):
                continue
            succeeded = {step.id for step in run.steps if step.status == "succeeded"}
            for state, definition in zip(run.steps, self.workflow.steps):
                if (state.status == "pending" and state.ready_at <= self.now
                        and all(dep in succeeded for dep in definition.needs)):
                    return run, state, definition
        return None

    def mark_run_failed(self, run: RunState) -> None:
        """Mark a run as failed and block all pending steps."""
        run.status = "failed"
        for step in run.steps:
            if step.status == "pending":
                step.status = "blocked"

    def try_execute_step(self, run: RunState, state: StepState, definition: StepDefinition) -> bool:
        """Execute or recover a single step. Returns True if action was taken."""
        key = (run.id, state.id)
        
        # Handle transition to running if this is the first attempt
        if state.status == "pending":
            state.status = "running"
            state.attempts += 1
        
        # Execute the service call
        outcome = self.service.execute(key, definition.amount, state.attempts, definition.failures)
        
        # Handle outcomes
        if outcome == "applied" or outcome == "replayed":
            state.status = "succeeded"
            # Check if run is complete
            if all(step.status == "succeeded" for step in run.steps):
                if run.status == "cancelling":
                    run.status = "cancelled"
                elif run.status == "active":
                    run.status = "succeeded"
            return True
        elif outcome == "transient":
            # Check if we've exhausted retries
            if state.attempts >= self.workflow.max_attempts:
                state.status = "failed"
                run.status = "failed"
                # Block all pending steps in this run
                for step in run.steps:
                    if step.status == "pending":
                        step.status = "blocked"
            else:
                # Schedule retry
                state.status = "pending"
                if self.now + self.workflow.retry_delay > TIME_LIMIT:
                    raise DomainError("TIME_OVERFLOW")
                state.ready_at = self.now + self.workflow.retry_delay
            return True
        
        return False

    def tick(self) -> None:
        action = self.select_action()
        if action is None:
            return
        run, state, definition = action
        self.try_execute_step(run, state, definition)

    def tick_with_crash(self, crash_at: str) -> None:
        """Execute a tick with crash checkpoints."""
        action = self.select_action()
        if action is None:
            # No eligible work - stay up if there's a checkpoint
            return
        
        run, state, definition = action
        key = (run.id, state.id)
        
        if crash_at == "after_begin":
            # Crash after recording the attempt but before service call
            if state.status == "pending":
                state.status = "running"
                state.attempts += 1
            self.up = False
        elif crash_at == "after_call":
            # Crash after service call but before committing to durable state
            if state.status == "pending":
                state.status = "running"
                state.attempts += 1
            
            # Execute the service call
            outcome = self.service.execute(key, definition.amount, state.attempts, definition.failures)
            self.up = False
            
            # Note: we don't apply the outcome to the step state since we crashed
            # The next tick will either replay or handle it

    def apply_cancel(self, run: RunState) -> None:
        """Apply cancellation logic to a run."""
        if run.status in ("succeeded", "failed", "cancelled"):
            # Terminal run, no-op
            return
        
        run.cancel_requested = True
        
        # Mark all pending steps as cancelled
        for step in run.steps:
            if step.status == "pending":
                step.status = "cancelled"
        
        # If no running step, mark run as cancelled
        has_running = any(step.status == "running" for step in run.steps)
        if not has_running:
            run.status = "cancelled"
        else:
            run.status = "cancelling"

    def reconcile_cancellation_with_crash(self, crash_at: str) -> None:
        """Reconcile a running step in a cancelling run via lookup with crash handling."""
        for run in self.runs:
            if run.status != "cancelling":
                continue
            
            for state, definition in zip(run.steps, self.workflow.steps):
                if state.status == "running":
                    key = (run.id, state.id)
                    
                    if crash_at == "after_begin":
                        # Crash before lookup, don't call service
                        self.up = False
                    elif crash_at == "after_call":
                        # Perform lookup and crash after, without applying result
                        outcome = self.service.lookup(key, state.attempts)
                        self.up = False
                        # Don't apply the outcome - it will be applied after restart/tick
                    return

    def reconcile_cancellation(self) -> None:
        """Reconcile a running step in a cancelling run via lookup."""
        for run in self.runs:
            if run.status != "cancelling":
                continue
            
            for state, definition in zip(run.steps, self.workflow.steps):
                if state.status == "running":
                    key = (run.id, state.id)
                    outcome = self.service.lookup(key, state.attempts)
                    
                    if outcome == "found":
                        state.status = "succeeded"
                    else:  # missing
                        state.status = "cancelled"
                    
                    run.status = "cancelled"
                    return

    def apply_v1(self, command: Command) -> None:
        if command.op == "start":
            require(self.up, "PROCESS_DOWN")
            require(all(run.id != command.run for run in self.runs), "DUPLICATE_RUN")
            self.runs.append(RunState(command.run, [StepState(step.id, ready_at=self.now)
                                                   for step in self.workflow.steps]))
        elif command.op == "tick":
            require(self.up, "PROCESS_DOWN")
            if command.crash_at == "after_begin":
                self.tick_with_crash("after_begin")
            elif command.crash_at == "after_call":
                self.tick_with_crash("after_call")
            else:
                self.tick()
        elif command.op == "advance":
            require(self.now + command.by <= TIME_LIMIT, "TIME_OVERFLOW")
            self.now += command.by
        elif command.op == "observe":
            self.observations.append(self.snapshot_v1())
        elif command.op == "crash":
            self.up = False
        elif command.op == "restart":
            require(not self.up, "PROCESS_UP")
            self.up = True
        elif command.op == "cancel":
            require(self.up, "PROCESS_DOWN")
            run = next((r for r in self.runs if r.id == command.run), None)
            require(run is not None, "UNKNOWN_RUN")
            self.apply_cancel(run)
        else:
            raise DomainError("UNSUPPORTED_FEATURE")

    # V2 (leased worker mode) methods
    def claim_v2(self, worker: str) -> int | None:
        """Claim work for a worker. Returns ticket or None."""
        require(worker in self.workers, "UNKNOWN_WORKER")
        require(self.workers[worker].up, "WORKER_DOWN")
        
        # Check if worker has a live lease
        for run in self.runs:
            for step in run.steps:
                if step.lease and step.lease.worker == worker and step.lease.expires > self.now:
                    raise DomainError("WORKER_BUSY")
        
        # First priority: expired lease (recovery)
        for run in self.runs:
            if run.status in ("active", "cancelling", "failing"):
                for state, definition in zip(run.steps, self.workflow.steps):
                    if state.status == "running" and state.lease and state.lease.expires <= self.now:
                        # Found an expired lease to reclaim
                        self.ticket_counter += 1
                        ticket = self.ticket_counter
                        kind = "lookup" if run.status in ("cancelling", "failing") else "execute"
                        expires = self.now + self.workflow.lease_duration
                        require(expires <= TIME_LIMIT, "TIME_OVERFLOW")
                        new_lease = Lease(worker, ticket, expires)
                        self.tickets[ticket] = TicketInfo(worker, (run.id, state.id), kind, state.attempts)
                        state.lease = new_lease
                        return ticket
        
        # Second priority: ready pending step
        for run in self.runs:
            if run.status != "active":
                continue
            succeeded = {step.id for step in run.steps if step.status == "succeeded"}
            for state, definition in zip(run.steps, self.workflow.steps):
                if (state.status == "pending" and state.ready_at <= self.now
                        and all(dep in succeeded for dep in definition.needs)):
                    # Start new work
                    state.status = "running"
                    state.attempts += 1
                    self.ticket_counter += 1
                    ticket = self.ticket_counter
                    expires = self.now + self.workflow.lease_duration
                    require(expires <= TIME_LIMIT, "TIME_OVERFLOW")
                    new_lease = Lease(worker, ticket, expires)
                    self.tickets[ticket] = TicketInfo(worker, (run.id, state.id), "execute", state.attempts)
                    state.lease = new_lease
                    return ticket
        
        return None

    def call_v2(self, worker: str, ticket: int) -> dict | None:
        """Make a service call for a ticket."""
        require(worker in self.workers, "UNKNOWN_WORKER")
        require(self.workers[worker].up, "WORKER_DOWN")
        require(ticket in self.tickets, "UNKNOWN_TICKET")
        
        ticket_info = self.tickets[ticket]
        require(ticket_info.worker == worker, "WRONG_WORKER")
        
        # Find the step
        run = next((r for r in self.runs if r.id == ticket_info.step_key[0]), None)
        require(run is not None, "UNKNOWN_RUN")
        step = next((s for s in run.steps if s.id == ticket_info.step_key[1]), None)
        require(step is not None, "UNKNOWN_STEP")
        
        # Check if ticket is current and live
        if step.lease is None or step.lease.ticket != ticket:
            return {"outcome": "stale"}
        if step.lease.expires <= self.now:
            return {"outcome": "stale"}
        
        # If this is the first call on this ticket, make the service call
        if ticket_info.response is None:
            definition = next(d for d in self.workflow.steps if d.id == step.id)
            key = ticket_info.step_key
            if ticket_info.kind == "execute":
                response = self.service.execute(key, definition.amount, ticket_info.attempt, definition.failures, 
                                               worker=worker, ticket=ticket)
            else:  # lookup
                response = self.service.lookup(key, ticket_info.attempt, worker=worker, ticket=ticket)
            ticket_info = ticket_info.__class__(ticket_info.worker, ticket_info.step_key, ticket_info.kind,
                                               ticket_info.attempt, response=response, response_delivered=False)
            self.tickets[ticket] = ticket_info
        
        # Return the saved response
        if ticket_info.kind == "execute":
            return {"kind": "execute", "outcome": ticket_info.response}
        else:
            return {"kind": "lookup", "outcome": ticket_info.response}

    def deliver_v2(self, ticket: int) -> bool:
        """Deliver/commit a service response."""
        require(ticket in self.tickets, "UNKNOWN_TICKET")
        
        ticket_info = self.tickets[ticket]
        
        # Find the step
        run = next((r for r in self.runs if r.id == ticket_info.step_key[0]), None)
        require(run is not None, "UNKNOWN_RUN")
        step = next((s for s in run.steps if s.id == ticket_info.step_key[1]), None)
        require(step is not None, "UNKNOWN_STEP")
        
        # Check fencing conditions
        if step.lease is None or step.lease.ticket != ticket:
            return False
        if step.lease.expires <= self.now:
            return False
        if not self.workers[ticket_info.worker].up:
            return False
        
        # Response must be available
        if ticket_info.response is None:
            return False
        
        # Cannot re-deliver
        if ticket_info.response_delivered:
            return False
        
        # Mark delivered
        ticket_info = ticket_info.__class__(ticket_info.worker, ticket_info.step_key, ticket_info.kind,
                                           ticket_info.attempt, response=ticket_info.response, response_delivered=True)
        self.tickets[ticket] = ticket_info
        
        # Apply the response
        definition = next(d for d in self.workflow.steps if d.id == step.id)
        response = ticket_info.response
        
        if response == "applied" or response == "replayed":
            step.status = "succeeded"
            step.lease = None
            # Check if run is complete
            if all(s.status == "succeeded" for s in run.steps):
                if run.status == "active":
                    run.status = "succeeded"
                elif run.status == "cancelling":
                    run.status = "cancelled"
            elif run.status == "failing":
                # Check if all running steps are now resolved
                if not any(s.status == "running" for s in run.steps):
                    run.status = "failed"
        elif response == "transient":
            if step.attempts >= self.workflow.max_attempts:
                # Exhausted retries
                step.status = "failed"
                step.lease = None
                # Check if we should go to failing or failed
                has_other_running = any(s.status == "running" for s in run.steps)
                if has_other_running:
                    run.status = "failing"
                    # Expire all other running leases
                    for s in run.steps:
                        if s.status == "running" and s.lease:
                            s.lease = Lease(s.lease.worker, s.lease.ticket, self.now)
                else:
                    run.status = "failed"
                    # Block all pending steps
                    for s in run.steps:
                        if s.status == "pending":
                            s.status = "blocked"
            else:
                # Schedule retry from delivery time
                step.status = "pending"
                step.lease = None
                new_ready_at = self.now + self.workflow.retry_delay
                require(new_ready_at <= TIME_LIMIT, "TIME_OVERFLOW")
                step.ready_at = new_ready_at
        elif response == "found":
            # Lookup found - mark step as succeeded
            step.status = "succeeded"
            step.lease = None
            if run.status == "failing":
                if not any(s.status == "running" for s in run.steps):
                    run.status = "failed"
            elif run.status == "cancelling":
                if not any(s.status == "running" for s in run.steps):
                    run.status = "cancelled"
        elif response == "missing":
            # Lookup missing - mark step as cancelled
            step.status = "cancelled"
            step.lease = None
            if run.status == "cancelling":
                if not any(s.status == "running" for s in run.steps):
                    run.status = "cancelled"
            elif run.status == "failing":
                if not any(s.status == "running" for s in run.steps):
                    run.status = "failed"
        
        return True

    def apply_cancel_v2(self, run: RunState) -> None:
        """Apply cancellation logic to a run in v2 mode."""
        if run.status in ("succeeded", "failed", "cancelled"):
            # Terminal run, no-op
            return
        
        run.cancel_requested = True
        
        # Mark all pending steps as cancelled
        for step in run.steps:
            if step.status == "pending":
                step.status = "cancelled"
        
        # Expire all running leases immediately
        for step in run.steps:
            if step.status == "running" and step.lease:
                step.lease = Lease(step.lease.worker, step.lease.ticket, self.now)
        
        # If no running step, mark run as cancelled
        has_running = any(step.status == "running" for step in run.steps)
        if not has_running:
            run.status = "cancelled"
        else:
            run.status = "cancelling"

    def apply_v2(self, command: Command) -> Any:
        if command.op == "start":
            require(all(run.id != command.run for run in self.runs), "DUPLICATE_RUN")
            self.runs.append(RunState(command.run, [StepState(step.id, ready_at=self.now)
                                                   for step in self.workflow.steps]))
            return None
        elif command.op == "advance":
            require(self.now + command.by <= TIME_LIMIT, "TIME_OVERFLOW")
            self.now += command.by
            return None
        elif command.op == "observe":
            return self.snapshot_v2()
        elif command.op == "crash":
            require(command.worker in self.workers, "UNKNOWN_WORKER")
            require(self.workers[command.worker].up, "WORKER_DOWN")
            self.workers[command.worker].up = False
            return None
        elif command.op == "restart":
            require(command.worker in self.workers, "UNKNOWN_WORKER")
            require(not self.workers[command.worker].up, "WORKER_UP")
            self.workers[command.worker].up = True
            return None
        elif command.op == "claim":
            ticket = self.claim_v2(command.worker)
            if ticket is None:
                return {"ticket": None}
            else:
                return {"ticket": ticket}
        elif command.op == "renew":
            require(command.worker in self.workers, "UNKNOWN_WORKER")
            require(self.workers[command.worker].up, "WORKER_DOWN")
            require(command.ticket in self.tickets, "UNKNOWN_TICKET")
            
            ticket_info = self.tickets[command.ticket]
            require(ticket_info.worker == command.worker, "WRONG_WORKER")
            
            # Find the step
            run = next((r for r in self.runs if r.id == ticket_info.step_key[0]), None)
            if run is None:
                return {"renewed": False}
            step = next((s for s in run.steps if s.id == ticket_info.step_key[1]), None)
            if step is None:
                return {"renewed": False}
            
            # Check if lease is current and live
            if step.lease is None or step.lease.ticket != command.ticket:
                return {"renewed": False}
            if step.lease.expires <= self.now:
                return {"renewed": False}
            
            # Renew the lease
            new_expires = self.now + self.workflow.lease_duration
            require(new_expires <= TIME_LIMIT, "TIME_OVERFLOW")
            step.lease = Lease(step.lease.worker, step.lease.ticket, new_expires)
            return {"renewed": True}
        elif command.op == "call":
            result = self.call_v2(command.worker, command.ticket)
            return result
        elif command.op == "deliver":
            committed = self.deliver_v2(command.ticket)
            return {"committed": committed}
        elif command.op == "cancel":
            run = next((r for r in self.runs if r.id == command.run), None)
            require(run is not None, "UNKNOWN_RUN")
            self.apply_cancel_v2(run)
            return None
        else:
            raise DomainError("UNSUPPORTED_FEATURE")

    def snapshot_v1(self) -> dict:
        # New containers at every level keep observations independent of mutable state.
        return {"now": self.now, "up": self.up, "runs": [
            {"id": run.id, "status": run.status, "cancel_requested": run.cancel_requested,
             "steps": [{"id": step.id, "status": step.status, "attempts": step.attempts,
                        "ready_at": step.ready_at} for step in run.steps]} for run in self.runs]}

    def snapshot_v2(self) -> dict:
        # V2 snapshot with workers
        workers_list = [{"id": w.id, "up": w.up} for w in self.workers.values()]
        runs_list = []
        for run in self.runs:
            steps_list = []
            for step in run.steps:
                step_obj = {"id": step.id, "status": step.status, "attempts": step.attempts,
                           "ready_at": step.ready_at}
                if step.lease:
                    step_obj["lease"] = {"worker": step.lease.worker, "ticket": step.lease.ticket,
                                        "expires": step.lease.expires}
                else:
                    step_obj["lease"] = None
                steps_list.append(step_obj)
            runs_list.append({"id": run.id, "status": run.status, "cancel_requested": run.cancel_requested,
                             "steps": steps_list})
        return {"now": self.now, "workers": workers_list, "runs": runs_list}

    def apply(self, command: Command) -> None:
        if self.v2:
            raise DomainError("UNSUPPORTED_FEATURE")
        
        if command.op == "start":
            require(self.up, "PROCESS_DOWN")
            require(all(run.id != command.run for run in self.runs), "DUPLICATE_RUN")
            self.runs.append(RunState(command.run, [StepState(step.id, ready_at=self.now)
                                                   for step in self.workflow.steps]))
        elif command.op == "tick":
            require(self.up, "PROCESS_DOWN")
            if command.crash_at == "after_begin":
                self.tick_with_crash("after_begin")
            elif command.crash_at == "after_call":
                self.tick_with_crash("after_call")
            else:
                self.tick()
        elif command.op == "advance":
            require(self.now + command.by <= TIME_LIMIT, "TIME_OVERFLOW")
            self.now += command.by
        elif command.op == "observe":
            self.observations.append(self.snapshot_v1())
        elif command.op == "crash":
            self.up = False
        elif command.op == "restart":
            require(not self.up, "PROCESS_UP")
            self.up = True
        elif command.op == "cancel":
            require(self.up, "PROCESS_DOWN")
            run = next((r for r in self.runs if r.id == command.run), None)
            require(run is not None, "UNKNOWN_RUN")
            self.apply_cancel(run)
        else:
            raise DomainError("UNSUPPORTED_FEATURE")

    def run(self) -> dict:
        if self.v2:
            # V2 mode
            for command in self.workflow.commands:
                result = self.apply_v2(command)
                self.results.append(result)
            
            final = self.snapshot_v2()
            final["calls"] = []
            for call in self.service.calls:
                call_obj = {"worker": call.worker, "ticket": call.ticket, "kind": call.kind, 
                           "key": list(call.key), "attempt": call.attempt, "outcome": call.outcome}
                final["calls"].append(call_obj)
            final["effects"] = [{"key": list(effect.key), "amount": effect.amount}
                               for effect in self.service.effects]
            
            return {"results": self.results, "final": final}
        else:
            # V1 mode
            for command in self.workflow.commands:
                # Handle special case: cancellation reconciliation for tick commands
                if command.op == "tick" and self.up:
                    # Check if we have a cancelling run with in-flight work
                    reconcile = False
                    for run in self.runs:
                        if run.status == "cancelling":
                            has_running = any(step.status == "running" for step in run.steps)
                            if has_running:
                                reconcile = True
                                break
                    
                    if reconcile:
                        if command.crash_at == "after_begin":
                            self.reconcile_cancellation_with_crash("after_begin")
                        elif command.crash_at == "after_call":
                            self.reconcile_cancellation_with_crash("after_call")
                        else:
                            self.reconcile_cancellation()
                    else:
                        # No cancelling run, proceed normally
                        if command.crash_at == "after_begin":
                            self.tick_with_crash("after_begin")
                        elif command.crash_at == "after_call":
                            self.tick_with_crash("after_call")
                        else:
                            self.tick()
                else:
                    self.apply(command)
            
            final = self.snapshot_v1()
            final["calls"] = [{"kind": call.kind, "key": list(call.key), "attempt": call.attempt,
                               "outcome": call.outcome} for call in self.service.calls]
            final["effects"] = [{"key": list(effect.key), "amount": effect.amount}
                                 for effect in self.service.effects]
            return {"observations": self.observations, "final": final}
