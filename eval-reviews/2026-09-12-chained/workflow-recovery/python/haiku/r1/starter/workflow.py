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


@dataclass(frozen=True)
class Workflow:
    steps: tuple[StepDefinition, ...]
    commands: tuple[Command, ...]
    max_attempts: int
    retry_delay: int


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


@dataclass
class StepState:
    id: str
    status: Literal["pending", "running", "succeeded", "failed", "blocked", "cancelled"] = "pending"
    attempts: int = 0
    ready_at: int = 0


@dataclass
class RunState:
    id: str
    steps: list[StepState]
    status: Literal["active", "succeeded", "failed", "cancelling", "cancelled"] = "active"
    cancel_requested: bool = False


@dataclass(frozen=True)
class ServiceCall:
    key: tuple[str, str]
    attempt: int
    kind: str = "execute"
    outcome: str = "applied"


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

    def execute(self, key: tuple[str, str], amount: int, attempt: int, failures_config: int) -> str:
        if key in self.effect_map:
            self.calls.append(ServiceCall(key, attempt, outcome="replayed"))
            return "replayed"
        
        counter = self.failure_counters.get(key, 0)
        if counter < failures_config:
            self.failure_counters[key] = counter + 1
            self.calls.append(ServiceCall(key, attempt, outcome="transient"))
            return "transient"
        
        self.calls.append(ServiceCall(key, attempt))
        self.effects.append(Effect(key, amount))
        self.effect_map[key] = Effect(key, amount)
        return "applied"
    
    def lookup(self, key: tuple[str, str], attempt: int) -> str:
        if key in self.effect_map:
            self.calls.append(ServiceCall(key, attempt, kind="lookup", outcome="found"))
            return "found"
        else:
            self.calls.append(ServiceCall(key, attempt, kind="lookup", outcome="missing"))
            return "missing"


class Simulator:
    def __init__(self, workflow: Workflow):
        self.workflow = workflow
        self.now = 0
        self.up = True
        self.runs: list[RunState] = []
        self.service = MockService()
        self.observations: list[dict] = []

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

    def apply(self, command: Command) -> None:
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
            self.observations.append(self.snapshot())
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

    def snapshot(self) -> dict:
        # New containers at every level keep observations independent of mutable state.
        return {"now": self.now, "up": self.up, "runs": [
            {"id": run.id, "status": run.status, "cancel_requested": run.cancel_requested,
             "steps": [{"id": step.id, "status": step.status, "attempts": step.attempts,
                        "ready_at": step.ready_at} for step in run.steps]} for run in self.runs]}

    def run(self) -> dict:
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
        
        final = self.snapshot()
        final["calls"] = [{"kind": call.kind, "key": list(call.key), "attempt": call.attempt,
                           "outcome": call.outcome} for call in self.service.calls]
        final["effects"] = [{"key": list(effect.key), "amount": effect.amount}
                             for effect in self.service.effects]
        return {"observations": self.observations, "final": final}
