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
