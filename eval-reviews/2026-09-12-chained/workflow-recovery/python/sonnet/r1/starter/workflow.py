"""Deterministic in-memory DAG runner with durable recovery, retries, and cancellation."""
from dataclasses import dataclass, field
import re
from typing import Any, Literal

TIME_LIMIT = 2_147_483_647

TERMINAL_RUN_STATUSES = ("succeeded", "failed", "cancelled")


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
    kind: str
    outcome: str


@dataclass(frozen=True)
class Effect:
    key: tuple[str, str]
    amount: int


@dataclass
class MockService:
    calls: list[ServiceCall] = field(default_factory=list)
    effects: list[Effect] = field(default_factory=list)
    _effect_keys: set = field(default_factory=set)
    _pending_failures: dict = field(default_factory=dict)

    def execute(self, key: tuple[str, str], amount: int, attempt: int, failures: int) -> str:
        if key in self._effect_keys:
            outcome = "replayed"
        else:
            seen = self._pending_failures.get(key, 0)
            if seen < failures:
                self._pending_failures[key] = seen + 1
                outcome = "transient"
            else:
                self.effects.append(Effect(key, amount))
                self._effect_keys.add(key)
                outcome = "applied"
        self.calls.append(ServiceCall(key, attempt, "execute", outcome))
        return outcome

    def lookup(self, key: tuple[str, str], attempt: int) -> str:
        outcome = "found" if key in self._effect_keys else "missing"
        self.calls.append(ServiceCall(key, attempt, "lookup", outcome))
        return outcome


class Simulator:
    def __init__(self, workflow: Workflow):
        self.workflow = workflow
        self.now = 0
        self.up = True
        self.runs: list[RunState] = []
        self.service = MockService()
        self.observations: list[dict] = []

    def find_run(self, run_id: str) -> RunState | None:
        return next((run for run in self.runs if run.id == run_id), None)

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

    def tick(self, crash_at: str | None) -> None:
        action = self.select_action()
        if action is None:
            return
        run, state, definition = action
        if state.status == "pending":
            state.status = "running"
            state.attempts += 1
        if run.status == "cancelling":
            self._reconcile(run, state, crash_at)
        else:
            self._advance_attempt(run, state, definition, crash_at)

    def _reconcile(self, run: RunState, state: StepState, crash_at: str | None) -> None:
        if crash_at == "after_begin":
            self.up = False
            return
        outcome = self.service.lookup((run.id, state.id), state.attempts)
        if crash_at == "after_call":
            self.up = False
            return
        state.status = "succeeded" if outcome == "found" else "cancelled"
        run.status = "cancelled"

    def _advance_attempt(self, run: RunState, state: StepState, definition: StepDefinition,
                         crash_at: str | None) -> None:
        if crash_at == "after_begin":
            self.up = False
            return
        outcome = self.service.execute((run.id, state.id), definition.amount, state.attempts,
                                       definition.failures)
        if crash_at == "after_call":
            self.up = False
            return
        if outcome in ("applied", "replayed"):
            state.status = "succeeded"
            if all(step.status == "succeeded" for step in run.steps):
                run.status = "succeeded"
        elif state.attempts < self.workflow.max_attempts:
            new_ready = self.now + self.workflow.retry_delay
            require(new_ready <= TIME_LIMIT, "TIME_OVERFLOW")
            state.status = "pending"
            state.ready_at = new_ready
        else:
            state.status = "failed"
            run.status = "failed"
            for step in run.steps:
                if step.status == "pending":
                    step.status = "blocked"

    def cancel(self, run: RunState) -> None:
        if run.status in TERMINAL_RUN_STATUSES:
            return
        run.cancel_requested = True
        for step in run.steps:
            if step.status == "pending":
                step.status = "cancelled"
        if any(step.status == "running" for step in run.steps):
            run.status = "cancelling"
        else:
            run.status = "cancelled"

    def apply(self, command: Command) -> None:
        if command.op == "start":
            require(self.up, "PROCESS_DOWN")
            require(all(run.id != command.run for run in self.runs), "DUPLICATE_RUN")
            self.runs.append(RunState(command.run, [StepState(step.id, ready_at=self.now)
                                                   for step in self.workflow.steps]))
        elif command.op == "tick":
            require(self.up, "PROCESS_DOWN")
            self.tick(command.crash_at)
        elif command.op == "advance":
            require(self.now + command.by <= TIME_LIMIT, "TIME_OVERFLOW")
            self.now += command.by
        elif command.op == "observe":
            self.observations.append(self.snapshot())
        elif command.op == "cancel":
            require(self.up, "PROCESS_DOWN")
            run = self.find_run(command.run)
            require(run is not None, "UNKNOWN_RUN")
            self.cancel(run)
        elif command.op == "crash":
            require(self.up, "PROCESS_DOWN")
            self.up = False
        elif command.op == "restart":
            require(not self.up, "PROCESS_UP")
            self.up = True
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
            self.apply(command)
        final = self.snapshot()
        final["calls"] = [{"kind": call.kind, "key": list(call.key), "attempt": call.attempt,
                           "outcome": call.outcome} for call in self.service.calls]
        final["effects"] = [{"key": list(effect.key), "amount": effect.amount}
                             for effect in self.service.effects]
        return {"observations": self.observations, "final": final}
