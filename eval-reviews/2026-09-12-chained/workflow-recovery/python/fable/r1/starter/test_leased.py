"""Agent-authored unit checks for checkpoint two; run with `python3 test_leased.py`."""
import unittest
from leased import LeasedSimulator, parse_leased_workflow
from workflow import DomainError

STEP = [{"id": "s", "needs": [], "amount": 5}]


def run(commands, steps=STEP, workers=("a", "b"), **config):
    payload = {"steps": steps, "commands": commands, "workers": list(workers), **config}
    return LeasedSimulator(parse_leased_workflow(payload)).run()


def error(commands, **kw):
    try:
        run(commands, **kw)
    except DomainError as exc:
        return exc.code
    return None


class LeasedTests(unittest.TestCase):
    def test_expired_ticket_is_stale_and_not_busy(self):
        result = run([{"op": "start", "run": "r"}, {"op": "claim", "worker": "a"},
                      {"op": "advance", "by": 5}, {"op": "renew", "worker": "a", "ticket": 1},
                      {"op": "call", "worker": "a", "ticket": 1}, {"op": "claim", "worker": "a"},
                      {"op": "call", "worker": "a", "ticket": 2}, {"op": "deliver", "ticket": 2}])
        self.assertEqual(result["results"][3:], [{"renewed": False}, {"outcome": "stale"},
                                                 {"ticket": 2}, {"kind": "execute", "outcome": "applied"},
                                                 {"committed": True}])
        self.assertEqual(result["final"]["runs"][0]["steps"][0]["attempts"], 1)

    def test_worker_validation_order(self):
        self.assertEqual(error([{"op": "claim", "worker": "z"}]), "UNKNOWN_WORKER")
        self.assertEqual(error([{"op": "crash", "worker": "a"}, {"op": "claim", "worker": "a"}]), "WORKER_DOWN")
        self.assertEqual(error([{"op": "crash", "worker": "a"}, {"op": "crash", "worker": "a"}]), "WORKER_DOWN")
        self.assertEqual(error([{"op": "restart", "worker": "a"}]), "WORKER_UP")
        self.assertEqual(error([{"op": "call", "worker": "a", "ticket": 1}]), "UNKNOWN_TICKET")
        self.assertEqual(error([{"op": "deliver", "ticket": 1}]), "UNKNOWN_TICKET")
        self.assertEqual(error([{"op": "start", "run": "r"}, {"op": "claim", "worker": "a"},
                                {"op": "call", "worker": "b", "ticket": 1}]), "WRONG_WORKER")
        self.assertEqual(error([{"op": "cancel", "run": "r"}]), "UNKNOWN_RUN")
        self.assertEqual(error([{"op": "start", "run": "r"}, {"op": "start", "run": "r"}]), "DUPLICATE_RUN")

    def test_ownership_stays_with_original_worker_after_reclaim(self):
        result = run([{"op": "start", "run": "r"}, {"op": "claim", "worker": "a"},
                      {"op": "advance", "by": 5}, {"op": "claim", "worker": "b"},
                      {"op": "renew", "worker": "a", "ticket": 1}])
        self.assertEqual(result["results"][-1], {"renewed": False})
        self.assertEqual(error([{"op": "start", "run": "r"}, {"op": "claim", "worker": "a"},
                                {"op": "advance", "by": 5}, {"op": "claim", "worker": "b"},
                                {"op": "renew", "worker": "b", "ticket": 1}]), "WRONG_WORKER")

    def test_repeated_cancel_keeps_lookup_lease(self):
        result = run([{"op": "start", "run": "r"}, {"op": "claim", "worker": "a"},
                      {"op": "cancel", "run": "r"}, {"op": "claim", "worker": "b"},
                      {"op": "cancel", "run": "r"}, {"op": "observe"},
                      {"op": "call", "worker": "b", "ticket": 2}, {"op": "deliver", "ticket": 2}])
        lease = result["results"][5]["runs"][0]["steps"][0]["lease"]
        self.assertEqual(lease, {"worker": "b", "ticket": 2, "expires": 5})
        self.assertEqual(result["results"][-1], {"committed": True})
        self.assertEqual(result["final"]["runs"][0]["status"], "cancelled")

    def test_failing_missing_blocks_and_rejects_cancel(self):
        steps = [{"id": "x", "needs": [], "amount": 1, "failures": 1}, {"id": "y", "needs": [], "amount": 2}]
        result = run([{"op": "start", "run": "r"}, {"op": "claim", "worker": "a"}, {"op": "claim", "worker": "b"},
                      {"op": "call", "worker": "a", "ticket": 1}, {"op": "deliver", "ticket": 1},
                      {"op": "cancel", "run": "r"}, {"op": "observe"}, {"op": "claim", "worker": "a"},
                      {"op": "call", "worker": "a", "ticket": 3}, {"op": "deliver", "ticket": 3}],
                     steps=steps, max_attempts=1)
        observed = result["results"][6]["runs"][0]
        self.assertEqual(observed["status"], "failing")
        self.assertFalse(observed["cancel_requested"])
        final = result["final"]["runs"][0]
        self.assertEqual(final["status"], "failed")
        self.assertEqual([s["status"] for s in final["steps"]], ["failed", "blocked"])
        self.assertEqual(result["final"]["calls"][-1]["outcome"], "missing")

    def test_transient_delivered_after_expiry_is_ignored(self):
        result = run([{"op": "start", "run": "r"}, {"op": "claim", "worker": "a"},
                      {"op": "call", "worker": "a", "ticket": 1}, {"op": "advance", "by": 5},
                      {"op": "deliver", "ticket": 1}, {"op": "observe"}],
                     steps=[{"id": "s", "needs": [], "amount": 5, "failures": 1}])
        step = result["results"][-1]["runs"][0]["steps"][0]
        self.assertEqual(result["results"][4], {"committed": False})
        self.assertEqual((step["status"], step["attempts"], step["ready_at"]), ("running", 1, 0))
        self.assertEqual(step["lease"], {"worker": "a", "ticket": 1, "expires": 5})

    def test_observations_are_immutable(self):
        result = run([{"op": "start", "run": "r"}, {"op": "observe"}, {"op": "claim", "worker": "a"}])
        self.assertEqual(result["results"][1]["runs"][0]["steps"][0]["status"], "pending")

    def test_overflow_and_input_errors(self):
        self.assertEqual(error([{"op": "advance", "by": 2147483647}, {"op": "start", "run": "r"},
                                {"op": "claim", "worker": "a"}]), "TIME_OVERFLOW")
        self.assertEqual(error([{"op": "tick"}]), "INVALID_INPUT")
        self.assertEqual(error([{"op": "claim", "worker": "a", "ticket": 1}]), "INVALID_INPUT")
        self.assertEqual(error([{"op": "deliver", "ticket": True}]), "INVALID_INPUT")
        self.assertEqual(error([{"op": "deliver", "ticket": 0}]), "INVALID_INPUT")
        self.assertEqual(error([], workers=[]), "INVALID_INPUT")
        self.assertEqual(error([], lease_duration=0), "INVALID_INPUT")
        self.assertEqual(error([{"op": "observe"}] * 2001), "INVALID_INPUT")
        self.assertEqual(error([], steps=[{"id": "s", "needs": ["s"], "amount": 1}]), "DEPENDENCY_CYCLE")


if __name__ == "__main__":
    unittest.main()
