#!/usr/bin/env python3
"""Agent-authored edge-case checks for the workflow-recovery extension.

Run after ./build.sh:  python3 extra_tests.py
Each case pipes one request into ./run.sh and compares the parsed response.
"""
import json, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))

def req(steps, commands, **cfg):
    inp = {"steps": steps, "commands": commands}
    inp.update(cfg)
    return {"protocol_version": 1, "task": "workflow-recovery", "input": inp}

def step(id, needs=(), amount=10, failures=None):
    s = {"id": id, "needs": list(needs), "amount": amount}
    if failures is not None:
        s["failures"] = failures
    return s

def st(id, status, attempts, ready_at):
    return {"id": id, "status": status, "attempts": attempts, "ready_at": ready_at}

def run(id, status, cancel, steps):
    return {"id": id, "status": status, "cancel_requested": cancel, "steps": steps}

def call(kind, key, attempt, outcome):
    return {"kind": kind, "key": key, "attempt": attempt, "outcome": outcome}

def ok(observations, now, up, runs, calls, effects):
    return {"ok": True, "result": {"observations": observations,
            "final": {"now": now, "up": up, "runs": runs, "calls": calls, "effects": effects}}}

def err(code):
    return {"ok": False, "error": {"code": code}}

CASES = [
    ("start-while-down", req([step("a")], [{"op": "crash"}, {"op": "start", "run": "r"}]), err("PROCESS_DOWN")),
    ("crash-while-down", req([step("a")], [{"op": "crash"}, {"op": "crash"}]), err("PROCESS_DOWN")),
    ("cancel-while-down-known-run", req([step("a")], [{"op": "start", "run": "r"}, {"op": "crash"}, {"op": "cancel", "run": "r"}]), err("PROCESS_DOWN")),
    ("duplicate-run-while-down-is-process-down", req([step("a")], [{"op": "start", "run": "r"}, {"op": "crash"}, {"op": "start", "run": "r"}]), err("PROCESS_DOWN")),
    ("repeated-cancel-of-cancelling-run",
     req([step("a"), step("b")], [{"op": "start", "run": "r"}, {"op": "tick", "crash_at": "after_begin"}, {"op": "restart"},
                                  {"op": "cancel", "run": "r"}, {"op": "cancel", "run": "r"}, {"op": "observe"}, {"op": "tick"}, {"op": "tick"}]),
     ok([{"now": 0, "up": True, "runs": [run("r", "cancelling", True, [st("a", "running", 1, 0), st("b", "cancelled", 0, 0)])]}],
        0, True, [run("r", "cancelled", True, [st("a", "cancelled", 1, 0), st("b", "cancelled", 0, 0)])],
        [call("lookup", ["r", "a"], 1, "missing")], [])),
    ("cancel-failed-run-keeps-flag-false",
     req([step("a", failures=1)], [{"op": "start", "run": "r"}, {"op": "tick"}, {"op": "cancel", "run": "r"}], max_attempts=1),
     ok([], 0, True, [run("r", "failed", False, [st("a", "failed", 1, 0)])], [call("execute", ["r", "a"], 1, "transient")], [])),
    ("exhaustion-during-recovery-reuses-attempt",
     req([step("a", failures=2), step("b", ["a"])], [{"op": "start", "run": "r"}, {"op": "tick", "crash_at": "after_call"}, {"op": "restart"}, {"op": "tick"}], max_attempts=1),
     ok([], 0, True, [run("r", "failed", False, [st("a", "failed", 1, 0), st("b", "blocked", 0, 0)])],
        [call("execute", ["r", "a"], 1, "transient"), call("execute", ["r", "a"], 1, "transient")], [])),
    ("lost-transient-then-cancel-then-lookup-crash-after-call",
     req([step("a", failures=1)], [{"op": "start", "run": "r"}, {"op": "tick", "crash_at": "after_call"}, {"op": "restart"},
                                  {"op": "cancel", "run": "r"}, {"op": "tick", "crash_at": "after_call"}, {"op": "restart"}, {"op": "tick"}]),
     ok([], 0, True, [run("r", "cancelled", True, [st("a", "cancelled", 1, 0)])],
        [call("execute", ["r", "a"], 1, "transient"), call("lookup", ["r", "a"], 1, "missing"), call("lookup", ["r", "a"], 1, "missing")], [])),
    ("uncommitted-transient-does-not-overflow",
     req([step("a", failures=1)], [{"op": "advance", "by": 2147483647}, {"op": "start", "run": "r"}, {"op": "tick", "crash_at": "after_call"}, {"op": "observe"}]),
     ok([{"now": 2147483647, "up": False, "runs": [run("r", "active", False, [st("a", "running", 1, 2147483647)])]}],
        2147483647, False, [run("r", "active", False, [st("a", "running", 1, 2147483647)])], [call("execute", ["r", "a"], 1, "transient")], [])),
    ("failure-counters-independent-across-runs",
     req([step("a", failures=1)], [{"op": "start", "run": "x"}, {"op": "start", "run": "y"}, {"op": "tick"}, {"op": "tick"}, {"op": "advance", "by": 2}, {"op": "tick"}, {"op": "tick"}]),
     ok([], 2, True, [run("x", "succeeded", False, [st("a", "succeeded", 2, 2)]), run("y", "succeeded", False, [st("a", "succeeded", 2, 2)])],
        [call("execute", ["x", "a"], 1, "transient"), call("execute", ["y", "a"], 1, "transient"),
         call("execute", ["x", "a"], 2, "applied"), call("execute", ["y", "a"], 2, "applied")],
        [{"key": ["x", "a"], "amount": 10}, {"key": ["y", "a"], "amount": 10}])),
    ("inflight-cancelling-run-precedes-ready-run",
     req([step("a")], [{"op": "start", "run": "x"}, {"op": "start", "run": "y"}, {"op": "tick", "crash_at": "after_call"}, {"op": "restart"},
                      {"op": "cancel", "run": "x"}, {"op": "tick"}, {"op": "tick"}]),
     ok([], 0, True, [run("x", "cancelled", True, [st("a", "succeeded", 1, 0)]), run("y", "succeeded", False, [st("a", "succeeded", 1, 0)])],
        [call("execute", ["x", "a"], 1, "applied"), call("lookup", ["x", "a"], 1, "found"), call("execute", ["y", "a"], 1, "applied")],
        [{"key": ["x", "a"], "amount": 10}, {"key": ["y", "a"], "amount": 10}])),
    ("boolean-failures-rejected", req([{"id": "a", "needs": [], "amount": 1, "failures": True}], []), err("INVALID_INPUT")),
    ("crash-at-wrong-type", req([step("a")], [{"op": "tick", "crash_at": 1}]), err("INVALID_INPUT")),
]

def main():
    failed = 0
    for name, request, expected in CASES:
        proc = subprocess.run([os.path.join(HERE, "run.sh")], input=json.dumps(request) + "\n",
                              capture_output=True, text=True)
        try:
            actual = json.loads(proc.stdout)
        except ValueError:
            actual = None
        if proc.returncode != 0 or actual != expected:
            failed += 1
            print(f"FAIL {name}\n  expected {json.dumps(expected, sort_keys=True)}\n  actual   {proc.stdout.strip()}\n  stderr   {proc.stderr.strip()}")
        else:
            print(f"PASS {name}")
    print(f"{len(CASES) - failed}/{len(CASES)} passed")
    sys.exit(1 if failed else 0)

if __name__ == "__main__":
    main()
