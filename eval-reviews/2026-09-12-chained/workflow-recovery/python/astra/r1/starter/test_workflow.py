"""Additional recovery boundary and validation regression checks."""
import unittest

from workflow import DomainError, Simulator, parse_workflow, TIME_LIMIT


class RecoveryTests(unittest.TestCase):
    def simulate(self, commands, **config):
        return Simulator(parse_workflow({
            "steps": [{"id": "s", "needs": [], "amount": 7, "failures": 2}],
            "commands": commands, **config,
        })).run()

    def test_lost_failures_do_not_exhaust_one_attempt_budget(self):
        result = self.simulate([
            {"op": "start", "run": "r"},
            {"op": "tick", "crash_at": "after_call"},
            {"op": "restart"},
            {"op": "tick", "crash_at": "after_call"},
            {"op": "restart"}, {"op": "tick"},
        ], max_attempts=1)
        final = result["final"]
        self.assertEqual([c["outcome"] for c in final["calls"]],
                         ["transient", "transient", "applied"])
        self.assertEqual([c["attempt"] for c in final["calls"]], [1, 1, 1])
        self.assertEqual(final["runs"][0]["status"], "succeeded")
        self.assertEqual(final["effects"], [{"key": ["r", "s"], "amount": 7}])

    def test_cancel_repeated_missing_lookup_after_lost_failure(self):
        result = self.simulate([
            {"op": "start", "run": "r"},
            {"op": "tick", "crash_at": "after_call"},
            {"op": "restart"}, {"op": "cancel", "run": "r"},
            {"op": "observe"},
            {"op": "tick", "crash_at": "after_call"},
            {"op": "restart"}, {"op": "cancel", "run": "r"},
            {"op": "tick"}, {"op": "tick", "crash_at": "after_begin"},
        ])
        self.assertEqual(result["observations"][0]["runs"][0]["status"], "cancelling")
        final = result["final"]
        self.assertTrue(final["up"])
        self.assertEqual(final["runs"][0]["status"], "cancelled")
        self.assertEqual(final["effects"], [])
        self.assertEqual([(c["kind"], c["outcome"]) for c in final["calls"]],
                         [("execute", "transient"), ("lookup", "missing"),
                          ("lookup", "missing")])

    def test_exhaustion_at_time_limit_needs_no_retry_deadline(self):
        result = self.simulate([
            {"op": "advance", "by": TIME_LIMIT},
            {"op": "start", "run": "r"}, {"op": "tick"},
        ], max_attempts=1)
        self.assertEqual(result["final"]["runs"][0]["status"], "failed")

    def test_static_validation_precedes_process_error(self):
        with self.assertRaises(DomainError) as caught:
            self.simulate([{"op": "crash"}, {"op": "tick"},
                           {"op": "advance", "by": True}])
        self.assertEqual(caught.exception.code, "INVALID_INPUT")


if __name__ == "__main__":
    unittest.main()
