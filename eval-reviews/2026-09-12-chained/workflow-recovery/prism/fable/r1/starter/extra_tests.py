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

# ---------------------------------------------------------------------------
# Checkpoint two: leased workers. Helpers mirror the leased result schema.

def lreq(steps, commands, workers=("a", "b"), **cfg):
    inp = {"steps": steps, "commands": commands, "workers": list(workers) if isinstance(workers, tuple) else workers}
    inp.update(cfg)
    return {"protocol_version": 1, "task": "workflow-recovery", "input": inp}

def lease(worker, ticket, expires):
    return {"worker": worker, "ticket": ticket, "expires": expires}

def lst(id, status, attempts, ready_at, lease=None):
    return {"id": id, "status": status, "attempts": attempts, "ready_at": ready_at, "lease": lease}

def wk(a=True, b=True):
    return [{"id": "a", "up": a}, {"id": "b", "up": b}]

def snap(now, runs, workers=None):
    return {"now": now, "workers": workers or wk(), "runs": runs}

def lcall(worker, ticket, kind, key, attempt, outcome):
    return {"worker": worker, "ticket": ticket, "kind": kind, "key": key, "attempt": attempt, "outcome": outcome}

def lok(results, now, runs, calls, effects, workers=None):
    final = dict(snap(now, runs, workers)); final["calls"] = calls; final["effects"] = effects
    return {"ok": True, "result": {"results": results, "final": final}}

def T(n): return {"ticket": n}
def C(b): return {"committed": b}
EX_APPLIED = {"kind": "execute", "outcome": "applied"}
EX_TRANSIENT = {"kind": "execute", "outcome": "transient"}
LK_FOUND = {"kind": "lookup", "outcome": "found"}
LK_MISSING = {"kind": "lookup", "outcome": "missing"}
STALE = {"outcome": "stale"}
S = [step("s", amount=25)]
START = {"op": "start", "run": "r"}
def claim(w): return {"op": "claim", "worker": w}
def call_(w, t): return {"op": "call", "worker": w, "ticket": t}
def deliver(t): return {"op": "deliver", "ticket": t}
def renew(w, t): return {"op": "renew", "worker": w, "ticket": t}
def adv(n): return {"op": "advance", "by": n}
OBS = {"op": "observe"}
CANCEL = {"op": "cancel", "run": "r"}

LEASED_CASES = [
    ("l-renew-expired-false",
     lreq(S, [START, claim("a"), adv(5), renew("a", 1), OBS]),
     lok([None, T(1), None, {"renewed": False}, snap(5, [run("r", "active", False, [lst("s", "running", 1, 0, lease("a", 1, 5))])])],
         5, [run("r", "active", False, [lst("s", "running", 1, 0, lease("a", 1, 5))])], [], [])),
    ("l-renew-wrong-worker", lreq(S, [START, claim("a"), renew("b", 1)]), err("WRONG_WORKER")),
    ("l-renew-unknown-ticket", lreq(S, [START, claim("a"), renew("a", 2)]), err("UNKNOWN_TICKET")),
    ("l-unknown-worker-before-ticket", lreq(S, [START, claim("a"), renew("c", 1)]), err("UNKNOWN_WORKER")),
    ("l-worker-down-before-ticket", lreq(S, [START, claim("a"), {"op": "crash", "worker": "a"}, call_("a", 1)]), err("WORKER_DOWN")),
    ("l-deliver-unknown-ticket", lreq(S, [deliver(7)]), err("UNKNOWN_TICKET")),
    ("l-crash-twice", lreq(S, [{"op": "crash", "worker": "a"}, {"op": "crash", "worker": "a"}]), err("WORKER_DOWN")),
    ("l-restart-while-up", lreq(S, [{"op": "restart", "worker": "a"}]), err("WORKER_UP")),
    ("l-crash-unknown-worker", lreq(S, [{"op": "crash", "worker": "z"}]), err("UNKNOWN_WORKER")),
    ("l-tick-rejected", lreq(S, [START, {"op": "tick"}]), err("INVALID_INPUT")),
    ("l-command-bound", lreq(S, [OBS] * 2001), err("INVALID_INPUT")),
    ("l-command-bound-ok-prefix-validated", lreq(S, [OBS] * 2000), lok([snap(0, [])] * 2000, 0, [], [], [])),
    ("l-lease-duration-zero", lreq(S, [], lease_duration=0), err("INVALID_INPUT")),
    ("l-lease-duration-bool", lreq(S, [], lease_duration=True), err("INVALID_INPUT")),
    ("l-ticket-bool", lreq(S, [START, claim("a"), call_("a", True)]), err("INVALID_INPUT")),
    ("l-ticket-zero", lreq(S, [deliver(0)]), err("INVALID_INPUT")),
    ("l-extra-input-field", lreq(S, [], foo=1), err("INVALID_INPUT")),
    ("l-empty-workers", lreq(S, [], workers=[]), err("INVALID_INPUT")),
    ("l-workers-not-array", lreq(S, [], workers="a"), err("INVALID_INPUT")),
    ("l-bad-worker-id", lreq(S, [], workers=["a b"]), err("INVALID_INPUT")),
    ("l-claim-missing-worker", lreq(S, [{"op": "claim"}]), err("INVALID_INPUT")),
    ("l-deliver-extra-field", lreq(S, [{"op": "deliver", "ticket": 1, "worker": "a"}]), err("INVALID_INPUT")),
    ("l-validation-before-execution", lreq(S, [{"op": "crash", "worker": "z"}, {"op": "tick"}]), err("INVALID_INPUT")),
    ("l-graph-after-shape", lreq([step("s"), step("s")], [{"op": "tick"}]), err("INVALID_INPUT")),
    ("l-duplicate-step-leased", lreq([step("s"), step("s")], []), err("DUPLICATE_STEP")),
    ("l-duplicate-run", lreq(S, [START, START]), err("DUPLICATE_RUN")),
    ("l-unknown-run-cancel", lreq(S, [CANCEL]), err("UNKNOWN_RUN")),
    ("l-deliver-before-call-false",
     lreq(S, [START, claim("a"), deliver(1), call_("a", 1), deliver(1)]),
     lok([None, T(1), C(False), EX_APPLIED, C(True)], 0, [run("r", "succeeded", False, [lst("s", "succeeded", 1, 0)])],
         [lcall("a", 1, "execute", ["r", "s"], 1, "applied")], [{"key": ["r", "s"], "amount": 25}])),
    ("l-stale-transient-keeps-attempt",
     lreq([step("s", amount=25, failures=1)], [START, claim("a"), call_("a", 1), adv(5), claim("b"), deliver(1), OBS, call_("b", 2), deliver(2)]),
     lok([None, T(1), EX_TRANSIENT, None, T(2), C(False),
          snap(5, [run("r", "active", False, [lst("s", "running", 1, 0, lease("b", 2, 10))])]), EX_APPLIED, C(True)],
         5, [run("r", "succeeded", False, [lst("s", "succeeded", 1, 0)])],
         [lcall("a", 1, "execute", ["r", "s"], 1, "transient"), lcall("b", 2, "execute", ["r", "s"], 1, "applied")],
         [{"key": ["r", "s"], "amount": 25}])),
    ("l-exhaustion-single-step",
     lreq([step("s", amount=25, failures=1)], [START, claim("a"), call_("a", 1), deliver(1), OBS, claim("a"), CANCEL, OBS], max_attempts=1),
     lok([None, T(1), EX_TRANSIENT, C(True), snap(0, [run("r", "failed", False, [lst("s", "failed", 1, 0)])]), T(None), None,
          snap(0, [run("r", "failed", False, [lst("s", "failed", 1, 0)])])],
         0, [run("r", "failed", False, [lst("s", "failed", 1, 0)])], [lcall("a", 1, "execute", ["r", "s"], 1, "transient")], [])),
    ("l-repeated-cancel-keeps-lookup-lease",
     lreq([step("x", amount=1), step("y", amount=2)],
          [START, claim("a"), claim("b"), CANCEL, claim("a"), CANCEL, call_("a", 3), deliver(3), OBS, claim("b"), call_("b", 4), deliver(4), OBS]),
     lok([None, T(1), T(2), None, T(3), None, LK_MISSING, C(True),
          snap(0, [run("r", "cancelling", True, [lst("x", "cancelled", 1, 0), lst("y", "running", 1, 0, lease("b", 2, 0))])]),
          T(4), LK_MISSING, C(True),
          snap(0, [run("r", "cancelled", True, [lst("x", "cancelled", 1, 0), lst("y", "cancelled", 1, 0)])])],
         0, [run("r", "cancelled", True, [lst("x", "cancelled", 1, 0), lst("y", "cancelled", 1, 0)])],
         [lcall("a", 3, "lookup", ["r", "x"], 1, "missing"), lcall("b", 4, "lookup", ["r", "y"], 1, "missing")], [])),
    ("l-failing-cancel-noop-missing-blocked",
     lreq([step("x", amount=1, failures=1), step("y", amount=2), step("z", ["x"], amount=3)],
          [START, claim("a"), claim("b"), call_("a", 1), deliver(1), OBS, CANCEL, claim("a"), call_("a", 3), deliver(3), OBS], max_attempts=1),
     lok([None, T(1), T(2), EX_TRANSIENT, C(True),
          snap(0, [run("r", "failing", False, [lst("x", "failed", 1, 0), lst("y", "running", 1, 0, lease("b", 2, 0)), lst("z", "blocked", 0, 0)])]),
          None, T(3), LK_MISSING, C(True),
          snap(0, [run("r", "failed", False, [lst("x", "failed", 1, 0), lst("y", "blocked", 1, 0), lst("z", "blocked", 0, 0)])])],
         0, [run("r", "failed", False, [lst("x", "failed", 1, 0), lst("y", "blocked", 1, 0), lst("z", "blocked", 0, 0)])],
         [lcall("a", 1, "execute", ["r", "x"], 1, "transient"), lcall("a", 3, "lookup", ["r", "y"], 1, "missing")], [])),
    ("l-claim-overflow", lreq(S, [adv(2147483647), START, claim("a")]), err("TIME_OVERFLOW")),
    ("l-retry-deadline-overflow-at-delivery",
     lreq([step("s", failures=1)], [adv(2147483646), START, claim("a"), call_("a", 1), deliver(1)], lease_duration=1), err("TIME_OVERFLOW")),
    ("l-renew-overflow", lreq(S, [adv(2147483640), START, claim("a"), adv(3), renew("a", 1)]), err("TIME_OVERFLOW")),
    ("l-own-expired-lease-reclaim",
     lreq(S, [START, claim("a"), adv(5), claim("a"), call_("a", 1), call_("a", 2), deliver(1), deliver(2), OBS]),
     lok([None, T(1), None, T(2), STALE, EX_APPLIED, C(False), C(True), snap(5, [run("r", "succeeded", False, [lst("s", "succeeded", 1, 0)])])],
         5, [run("r", "succeeded", False, [lst("s", "succeeded", 1, 0)])],
         [lcall("a", 2, "execute", ["r", "s"], 1, "applied")], [{"key": ["r", "s"], "amount": 25}])),
    ("l-wrong-worker-after-reclaim", lreq(S, [START, claim("a"), adv(5), claim("b"), call_("b", 1)]), err("WRONG_WORKER")),
    ("l-expired-running-precedes-pending",
     lreq([step("p", amount=1), step("q", amount=2)], [{"op": "start", "run": "x"}, {"op": "start", "run": "y"}, claim("a"), adv(5), claim("b"), OBS]),
     lok([None, None, T(1), None, T(2),
          snap(5, [run("x", "active", False, [lst("p", "running", 1, 0, lease("b", 2, 10)), lst("q", "pending", 0, 0)]),
                   run("y", "active", False, [lst("p", "pending", 0, 0), lst("q", "pending", 0, 0)])])],
         5, [run("x", "active", False, [lst("p", "running", 1, 0, lease("b", 2, 10)), lst("q", "pending", 0, 0)]),
             run("y", "active", False, [lst("p", "pending", 0, 0), lst("q", "pending", 0, 0)])], [], [])),
    ("l-expired-lookup-response-not-committed",
     lreq(S, [START, claim("a"), call_("a", 1), CANCEL, claim("b"), call_("b", 2), adv(5), deliver(2), claim("a"), call_("a", 3), deliver(3), OBS]),
     lok([None, T(1), EX_APPLIED, None, T(2), LK_FOUND, None, C(False), T(3), LK_FOUND, C(True),
          snap(5, [run("r", "cancelled", True, [lst("s", "succeeded", 1, 0)])])],
         5, [run("r", "cancelled", True, [lst("s", "succeeded", 1, 0)])],
         [lcall("a", 1, "execute", ["r", "s"], 1, "applied"), lcall("b", 2, "lookup", ["r", "s"], 1, "found"), lcall("a", 3, "lookup", ["r", "s"], 1, "found")],
         [{"key": ["r", "s"], "amount": 25}])),
    ("l-cancel-pending-only-immediate",
     lreq(S, [START, CANCEL, OBS, claim("a")]),
     lok([None, None, snap(0, [run("r", "cancelled", True, [lst("s", "cancelled", 0, 0)])]), T(None)],
         0, [run("r", "cancelled", True, [lst("s", "cancelled", 0, 0)])], [], [])),
    ("l-renew-extends-and-blocks-reclaim",
     lreq(S, [START, claim("a"), adv(4), renew("a", 1), adv(1), claim("b"), OBS]),
     lok([None, T(1), None, {"renewed": True}, None, T(None), snap(5, [run("r", "active", False, [lst("s", "running", 1, 0, lease("a", 1, 9))])])],
         5, [run("r", "active", False, [lst("s", "running", 1, 0, lease("a", 1, 9))])], [], [])),
    ("l-call-after-delivery-stale",
     lreq(S, [START, claim("a"), call_("a", 1), deliver(1), call_("a", 1)]),
     lok([None, T(1), EX_APPLIED, C(True), STALE], 0, [run("r", "succeeded", False, [lst("s", "succeeded", 1, 0)])],
         [lcall("a", 1, "execute", ["r", "s"], 1, "applied")], [{"key": ["r", "s"], "amount": 25}])),
    ("l-deliver-down-then-expired",
     lreq(S, [START, claim("a"), call_("a", 1), {"op": "crash", "worker": "a"}, deliver(1), adv(5), {"op": "restart", "worker": "a"}, deliver(1), OBS]),
     lok([None, T(1), EX_APPLIED, None, C(False), None, None, C(False),
          snap(5, [run("r", "active", False, [lst("s", "running", 1, 0, lease("a", 1, 5))])])],
         5, [run("r", "active", False, [lst("s", "running", 1, 0, lease("a", 1, 5))])],
         [lcall("a", 1, "execute", ["r", "s"], 1, "applied")], [{"key": ["r", "s"], "amount": 25}])),
    ("legacy-rejects-leased-command", req(S, [{"op": "claim", "worker": "a"}]), err("INVALID_INPUT")),
    ("legacy-rejects-lease-duration", req(S, [], lease_duration=5), err("INVALID_INPUT")),
]
CASES = CASES + LEASED_CASES

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
