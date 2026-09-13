#!/usr/bin/env python3
"""Extra traces beyond the public suite, checked against the built adapter.

Run ./build.sh first, then `python3 selftest.py` from this directory.
"""
import json
import subprocess
import sys
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def ask(request_input):
    request = {"protocol_version": 1, "task": "workflow-recovery", "input": request_input}
    proc = subprocess.run(
        [os.path.join(HERE, "run.sh")],
        input=json.dumps(request) + "\n",
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def error(code):
    return {"ok": False, "error": {"code": code}}


def call(kind, run, step, attempt, outcome):
    return {"kind": kind, "key": [run, step], "attempt": attempt, "outcome": outcome}


def step(sid, status, attempts, ready_at):
    return {"id": sid, "status": status, "attempts": attempts, "ready_at": ready_at}


def run_state(rid, status, cancel_requested, steps):
    return {"id": rid, "status": status, "cancel_requested": cancel_requested, "steps": steps}


def lease(worker, ticket, expires):
    return {"worker": worker, "ticket": ticket, "expires": expires}


def lstep(sid, status, attempts, ready_at, held=None):
    return {"id": sid, "status": status, "attempts": attempts, "ready_at": ready_at, "lease": held}


def workers(*ids):
    return [{"id": wid, "up": True} for wid in ids]


def lcall(worker, ticket, kind, run, sid, attempt, outcome):
    return {"worker": worker, "ticket": ticket, "kind": kind, "key": [run, sid],
            "attempt": attempt, "outcome": outcome}


def leased(results, now, ws, runs, calls, effects):
    return {"ok": True, "result": {"results": results,
        "final": {"now": now, "workers": ws, "runs": runs, "calls": calls, "effects": effects}}}


TICKET = lambda n: {"ticket": n}
RECEIPT = lambda kind, outcome: {"kind": kind, "outcome": outcome}
COMMITTED = lambda flag: {"committed": flag}


ONE = [{"id": "a", "needs": [], "amount": 5}]
FLAKY = [{"id": "a", "needs": [], "amount": 5, "failures": 1}]

CASES = [
    (
        "crash-before-start-is-process-down",
        {"steps": ONE, "commands": [{"op": "crash"}, {"op": "start", "run": "r"}]},
        error("PROCESS_DOWN"),
    ),
    (
        "double-crash-is-process-down",
        {"steps": ONE, "commands": [{"op": "crash"}, {"op": "crash"}]},
        error("PROCESS_DOWN"),
    ),
    (
        "cancelled-run-id-is-not-reusable",
        {
            "steps": ONE,
            "commands": [
                {"op": "start", "run": "r"},
                {"op": "cancel", "run": "r"},
                {"op": "start", "run": "r"},
            ],
        },
        error("DUPLICATE_RUN"),
    ),
    (
        "lost-transient-then-cancel-reconciles-missing",
        {
            "steps": FLAKY,
            "commands": [
                {"op": "start", "run": "r"},
                {"op": "tick", "crash_at": "after_call"},
                {"op": "restart"},
                {"op": "cancel", "run": "r"},
                {"op": "cancel", "run": "r"},
                {"op": "tick"},
                {"op": "tick"},
            ],
        },
        {
            "ok": True,
            "result": {
                "observations": [],
                "final": {
                    "now": 0,
                    "up": True,
                    "runs": [run_state("r", "cancelled", True, [step("a", "cancelled", 1, 0)])],
                    "calls": [
                        call("execute", "r", "a", 1, "transient"),
                        call("lookup", "r", "a", 1, "missing"),
                    ],
                    "effects": [],
                },
            },
        },
    ),
    (
        "recovery-may-exceed-attempt-budget-in-calls",
        {
            "steps": FLAKY,
            "max_attempts": 1,
            "commands": [
                {"op": "start", "run": "r"},
                {"op": "tick", "crash_at": "after_call"},
                {"op": "restart"},
                {"op": "tick"},
            ],
        },
        {
            "ok": True,
            "result": {
                "observations": [],
                "final": {
                    "now": 0,
                    "up": True,
                    "runs": [run_state("r", "succeeded", False, [step("a", "succeeded", 1, 0)])],
                    "calls": [
                        call("execute", "r", "a", 1, "transient"),
                        call("execute", "r", "a", 1, "applied"),
                    ],
                    "effects": [{"key": ["r", "a"], "amount": 5}],
                },
            },
        },
    ),
    (
        "independent-runs-keep-separate-failure-counters",
        {
            "steps": FLAKY,
            "max_attempts": 2,
            "commands": [
                {"op": "start", "run": "r1"},
                {"op": "start", "run": "r2"},
                {"op": "tick"},
                {"op": "tick"},
                {"op": "advance", "by": 2},
                {"op": "tick"},
                {"op": "tick"},
            ],
        },
        {
            "ok": True,
            "result": {
                "observations": [],
                "final": {
                    "now": 2,
                    "up": True,
                    "runs": [
                        run_state("r1", "succeeded", False, [step("a", "succeeded", 2, 2)]),
                        run_state("r2", "succeeded", False, [step("a", "succeeded", 2, 2)]),
                    ],
                    "calls": [
                        call("execute", "r1", "a", 1, "transient"),
                        call("execute", "r2", "a", 1, "transient"),
                        call("execute", "r1", "a", 2, "applied"),
                        call("execute", "r2", "a", 2, "applied"),
                    ],
                    "effects": [
                        {"key": ["r1", "a"], "amount": 5},
                        {"key": ["r2", "a"], "amount": 5},
                    ],
                },
            },
        },
    ),
    (
        "cancelling-run-lookup-found-does-not-succeed-the-run",
        {
            "steps": ONE,
            "commands": [
                {"op": "start", "run": "r"},
                {"op": "tick", "crash_at": "after_call"},
                {"op": "restart"},
                {"op": "cancel", "run": "r"},
                {"op": "tick"},
                {"op": "tick"},
            ],
        },
        {
            "ok": True,
            "result": {
                "observations": [],
                "final": {
                    "now": 0,
                    "up": True,
                    "runs": [run_state("r", "cancelled", True, [step("a", "succeeded", 1, 0)])],
                    "calls": [
                        call("execute", "r", "a", 1, "applied"),
                        call("lookup", "r", "a", 1, "found"),
                    ],
                    "effects": [{"key": ["r", "a"], "amount": 5}],
                },
            },
        },
    ),
]

LEASED_CASES = [
    (
        "leased-unknown-worker",
        {"steps": ONE, "workers": ["a"], "commands": [{"op": "claim", "worker": "z"}]},
        error("UNKNOWN_WORKER"),
    ),
    (
        "leased-availability-before-ticket-existence",
        {"steps": ONE, "workers": ["a"], "commands": [
            {"op": "crash", "worker": "a"}, {"op": "call", "worker": "a", "ticket": 9}]},
        error("WORKER_DOWN"),
    ),
    (
        "leased-unknown-ticket-before-ownership",
        {"steps": ONE, "workers": ["a"], "commands": [{"op": "call", "worker": "a", "ticket": 9}]},
        error("UNKNOWN_TICKET"),
    ),
    (
        "leased-wrong-worker",
        {"steps": ONE, "workers": ["a", "b"], "commands": [
            {"op": "start", "run": "r"}, {"op": "claim", "worker": "a"},
            {"op": "renew", "worker": "b", "ticket": 1}]},
        error("WRONG_WORKER"),
    ),
    (
        "leased-tick-is-not-accepted",
        {"steps": ONE, "workers": ["a"], "commands": [{"op": "tick"}]},
        error("INVALID_INPUT"),
    ),
    (
        "leased-crash-and-restart-guards",
        {"steps": ONE, "workers": ["a"], "commands": [{"op": "restart", "worker": "a"}]},
        error("WORKER_UP"),
    ),
    (
        "leased-double-crash",
        {"steps": ONE, "workers": ["a"], "commands": [
            {"op": "crash", "worker": "a"}, {"op": "crash", "worker": "a"}]},
        error("WORKER_DOWN"),
    ),
    (
        "leased-unknown-run-cancel",
        {"steps": ONE, "workers": ["a"], "commands": [{"op": "cancel", "run": "r"}]},
        error("UNKNOWN_RUN"),
    ),
    (
        "leased-claim-overflow",
        {"steps": ONE, "workers": ["a"], "commands": [
            {"op": "start", "run": "r"}, {"op": "advance", "by": 2147483647},
            {"op": "claim", "worker": "a"}]},
        error("TIME_OVERFLOW"),
    ),
    (
        "leased-command-budget",
        {"steps": ONE, "workers": ["a"], "commands": [{"op": "observe"}] * 2001},
        error("INVALID_INPUT"),
    ),
    (
        "leased-expired-lease-stays-visible-and-renew-fails",
        {"steps": ONE, "workers": ["a"], "commands": [
            {"op": "start", "run": "r"}, {"op": "claim", "worker": "a"},
            {"op": "advance", "by": 5}, {"op": "renew", "worker": "a", "ticket": 1},
            {"op": "observe"}]},
        leased([None, TICKET(1), None, {"renewed": False},
                {"now": 5, "workers": workers("a"),
                 "runs": [run_state("r", "active", False, [lstep("a", "running", 1, 0, lease("a", 1, 5))])]}],
               5, workers("a"),
               [run_state("r", "active", False, [lstep("a", "running", 1, 0, lease("a", 1, 5))])],
               [], []),
    ),
    (
        "leased-own-expired-lease-is-not-busy",
        {"steps": ONE, "workers": ["a"], "commands": [
            {"op": "start", "run": "r"}, {"op": "claim", "worker": "a"},
            {"op": "advance", "by": 5}, {"op": "claim", "worker": "a"},
            {"op": "call", "worker": "a", "ticket": 2}, {"op": "deliver", "ticket": 2}]},
        leased([None, TICKET(1), None, TICKET(2), RECEIPT("execute", "applied"), COMMITTED(True)],
               5, workers("a"),
               [run_state("r", "succeeded", False, [lstep("a", "succeeded", 1, 0)])],
               [lcall("a", 2, "execute", "r", "a", 1, "applied")],
               [{"key": ["r", "a"], "amount": 5}]),
    ),
    (
        "leased-stale-transient-cannot-commit",
        {"steps": FLAKY, "workers": ["a", "b"], "commands": [
            {"op": "start", "run": "r"}, {"op": "claim", "worker": "a"},
            {"op": "call", "worker": "a", "ticket": 1}, {"op": "advance", "by": 5},
            {"op": "claim", "worker": "b"}, {"op": "deliver", "ticket": 1},
            {"op": "call", "worker": "b", "ticket": 2}, {"op": "deliver", "ticket": 2}]},
        leased([None, TICKET(1), RECEIPT("execute", "transient"), None, TICKET(2), COMMITTED(False),
                RECEIPT("execute", "applied"), COMMITTED(True)],
               5, workers("a", "b"),
               [run_state("r", "succeeded", False, [lstep("a", "succeeded", 1, 0)])],
               [lcall("a", 1, "execute", "r", "a", 1, "transient"),
                lcall("b", 2, "execute", "r", "a", 1, "applied")],
               [{"key": ["r", "a"], "amount": 5}]),
    ),
    (
        "leased-repeated-cancel-keeps-lookup-lease",
        {"steps": ONE, "workers": ["a", "b"], "commands": [
            {"op": "start", "run": "r"}, {"op": "claim", "worker": "a"},
            {"op": "cancel", "run": "r"}, {"op": "claim", "worker": "b"},
            {"op": "cancel", "run": "r"}, {"op": "observe"},
            {"op": "call", "worker": "b", "ticket": 2}, {"op": "deliver", "ticket": 2}]},
        leased([None, TICKET(1), None, TICKET(2), None,
                {"now": 0, "workers": workers("a", "b"),
                 "runs": [run_state("r", "cancelling", True,
                                    [lstep("a", "running", 1, 0, lease("b", 2, 5))])]},
                RECEIPT("lookup", "missing"), COMMITTED(True)],
               0, workers("a", "b"),
               [run_state("r", "cancelled", True, [lstep("a", "cancelled", 1, 0)])],
               [lcall("b", 2, "lookup", "r", "a", 1, "missing")],
               []),
    ),
    (
        "leased-failing-run-ignores-cancellation",
        {"steps": [{"id": "x", "needs": [], "amount": 1, "failures": 1},
                   {"id": "y", "needs": [], "amount": 2}],
         "workers": ["a", "b"], "max_attempts": 1, "commands": [
            {"op": "start", "run": "r"}, {"op": "claim", "worker": "a"},
            {"op": "claim", "worker": "b"}, {"op": "call", "worker": "a", "ticket": 1},
            {"op": "deliver", "ticket": 1}, {"op": "cancel", "run": "r"},
            {"op": "claim", "worker": "a"}, {"op": "call", "worker": "a", "ticket": 3},
            {"op": "deliver", "ticket": 3}]},
        leased([None, TICKET(1), TICKET(2), RECEIPT("execute", "transient"), COMMITTED(True), None,
                TICKET(3), RECEIPT("lookup", "missing"), COMMITTED(True)],
               0, workers("a", "b"),
               [run_state("r", "failed", False,
                          [lstep("x", "failed", 1, 0), lstep("y", "blocked", 1, 0)])],
               [lcall("a", 1, "execute", "r", "x", 1, "transient"),
                lcall("a", 3, "lookup", "r", "y", 1, "missing")],
               []),
    ),
    (
        "leased-runs-are-independent",
        {"steps": ONE, "workers": ["a", "b"], "commands": [
            {"op": "start", "run": "r1"}, {"op": "start", "run": "r2"},
            {"op": "claim", "worker": "a"}, {"op": "claim", "worker": "b"},
            {"op": "call", "worker": "b", "ticket": 2}, {"op": "deliver", "ticket": 2},
            {"op": "observe"}]},
        leased([None, None, TICKET(1), TICKET(2), RECEIPT("execute", "applied"), COMMITTED(True),
                {"now": 0, "workers": workers("a", "b"),
                 "runs": [run_state("r1", "active", False, [lstep("a", "running", 1, 0, lease("a", 1, 5))]),
                          run_state("r2", "succeeded", False, [lstep("a", "succeeded", 1, 0)])]}],
               0, workers("a", "b"),
               [run_state("r1", "active", False, [lstep("a", "running", 1, 0, lease("a", 1, 5))]),
                run_state("r2", "succeeded", False, [lstep("a", "succeeded", 1, 0)])],
               [lcall("b", 2, "execute", "r2", "a", 1, "applied")],
               [{"key": ["r2", "a"], "amount": 5}]),
    ),
]

CASES = CASES + LEASED_CASES


def main():
    failed = 0
    for name, request_input, expected in CASES:
        actual = ask(request_input)
        if actual == expected:
            print("PASS", name)
        else:
            failed += 1
            print("FAIL", name)
            print("  expected:", json.dumps(expected, sort_keys=True))
            print("  actual:  ", json.dumps(actual, sort_keys=True))
    print(f"{len(CASES) - failed}/{len(CASES)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
