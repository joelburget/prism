"""Append-only baseline ledger: validation, domain events, and prefix projections."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import json
import re
import sys
from typing import Any


class OperationError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code


def require(condition: bool, code: str = "INVALID_OPERATION") -> None:
    if not condition:
        raise OperationError(code)


def identifier(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9-]{0,39}", value) is not None


def integer(value: Any, low: int, high: int | None = None) -> bool:
    return type(value) is int and value >= low and (high is None or value <= high)


@dataclass(frozen=True)
class Allocation:
    invoice: str
    amount: int


@dataclass(frozen=True)
class IssueInvoice:
    key: str
    period: int
    invoice: str
    total: int


@dataclass(frozen=True)
class Payment:
    key: str
    period: int
    allocations: tuple[Allocation, ...]


@dataclass(frozen=True)
class Close:
    key: str
    through: int


@dataclass(frozen=True)
class Read:
    kind: str
    as_of: int | None


Mutation = IssueInvoice | Payment | Close
Operation = Mutation | Read


def parse_operation(raw: Any) -> Operation:
    require(type(raw) is dict)
    kind = raw.get("op")
    if kind in ("report", "audit"):
        require(set(raw) in ({"op"}, {"op", "as_of"}))
        require("as_of" not in raw or integer(raw["as_of"], 0))
        return Read(kind, raw.get("as_of"))
    fields = {
        "invoice": {"op", "key", "period", "invoice", "total"},
        "payment": {"op", "key", "period", "allocations"},
        "close": {"op", "key", "through"},
    }
    require(isinstance(kind, str) and kind in fields)
    require(set(raw) == fields[kind])
    require(identifier(raw["key"]))
    if kind == "close":
        require(integer(raw["through"], 1, 1200))
        return Close(raw["key"], raw["through"])
    require(integer(raw["period"], 1, 1200))
    if kind == "invoice":
        require(identifier(raw["invoice"]) and integer(raw["total"], 1, 1_000_000_000))
        return IssueInvoice(raw["key"], raw["period"], raw["invoice"], raw["total"])
    allocations = raw["allocations"]
    require(type(allocations) is list and 1 <= len(allocations) <= 100)
    parsed: list[Allocation] = []
    seen: set[str] = set()
    for item in allocations:
        require(type(item) is dict and set(item) == {"invoice", "amount"})
        require(identifier(item["invoice"]) and integer(item["amount"], 1, 1_000_000_000))
        require(item["invoice"] not in seen)
        seen.add(item["invoice"])
        parsed.append(Allocation(item["invoice"], item["amount"]))
    return Payment(raw["key"], raw["period"], tuple(parsed))


@dataclass(frozen=True)
class Event:
    event_id: int
    mutation: Mutation
    payload: dict[str, Any]


@dataclass
class InvoiceBalance:
    total: int
    paid: int = 0


@dataclass
class Projection:
    closed_through: int = 0
    invoices: dict[str, InvoiceBalance] = field(default_factory=dict)

    def apply(self, mutation: Mutation) -> None:
        if isinstance(mutation, IssueInvoice):
            self.invoices[mutation.invoice] = InvoiceBalance(mutation.total)
        elif isinstance(mutation, Payment):
            for allocation in mutation.allocations:
                self.invoices[allocation.invoice].paid += allocation.amount
        else:
            self.closed_through = mutation.through

    def report(self) -> dict[str, Any]:
        rows = []
        for name, balance in sorted(self.invoices.items()):
            due = balance.total - balance.paid
            rows.append({"invoice": name, "total": balance.total, "paid": balance.paid,
                         "refunded": 0, "due": due,
                         "status": "unpaid" if balance.paid == 0 else "paid" if due == 0 else "partially_paid"})
        return {"closed_through": self.closed_through, "invoices": rows,
                "cash": sum(row["paid"] for row in rows),
                "outstanding": sum(row["due"] for row in rows)}


class Ledger:
    def __init__(self) -> None:
        self.events: list[Event] = []
        self.successful_keys: dict[str, Event] = {}
        self.current = Projection()

    def validate_mutation(self, mutation: Mutation) -> None:
        if isinstance(mutation, Close):
            require(mutation.through > self.current.closed_through, "INVALID_CLOSE")
            return
        require(mutation.period > self.current.closed_through, "CLOSED_PERIOD")
        if isinstance(mutation, IssueInvoice):
            require(mutation.invoice not in self.current.invoices, "DUPLICATE_INVOICE")
        else:
            # Separate passes make unknown invoices win regardless of array order.
            require(all(a.invoice in self.current.invoices for a in mutation.allocations), "UNKNOWN_INVOICE")
            require(all(a.amount <= self.current.invoices[a.invoice].total - self.current.invoices[a.invoice].paid
                        for a in mutation.allocations), "OVERPAYMENT")

    def execute(self, raw: Any) -> dict[str, Any]:
        try:
            operation = parse_operation(raw)
            if isinstance(operation, Read):
                end = len(self.events) if operation.as_of is None else operation.as_of
                require(end <= len(self.events), "INVALID_AS_OF")
                prefix = self.events[:end]
                if operation.kind == "audit":
                    result = {"events": [{"event_id": e.event_id, "operation": deepcopy(e.payload)} for e in prefix]}
                else:
                    snapshot = Projection()
                    for event in prefix:
                        snapshot.apply(event.mutation)
                    result = snapshot.report()
            elif operation.key in self.successful_keys:
                original = self.successful_keys[operation.key]
                require(raw == original.payload, "KEY_CONFLICT")
                result = {"event_id": original.event_id, "replayed": True}
            else:
                self.validate_mutation(operation)
                event = Event(len(self.events) + 1, operation, deepcopy(raw))
                # Commit only after every validation pass has succeeded.
                self.current.apply(operation)
                self.events.append(event)
                self.successful_keys[operation.key] = event
                result = {"event_id": event.event_id, "replayed": False}
            return {"ok": True, "result": result}
        except OperationError as error:
            return {"ok": False, "error": {"code": error.code}}


def run(request: Any) -> dict[str, Any]:
    task_input = request.get("input") if type(request) is dict else None
    if (type(task_input) is not dict or set(task_input) != {"operations"}
            or type(task_input["operations"]) is not list or len(task_input["operations"]) > 500):
        return {"ok": False, "error": {"code": "INVALID_INPUT"}}
    ledger = Ledger()
    return {"ok": True, "result": {"results": [ledger.execute(op) for op in task_input["operations"]]}}


if __name__ == "__main__":
    print(json.dumps(run(json.loads(sys.stdin.readline())), separators=(",", ":")))
