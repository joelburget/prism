"""Agent-authored unit checks for the extension; run with `python3 test_workflow.py`."""
import unittest
from workflow import DomainError, Simulator, parse_workflow


def run(steps, commands, **config):
    return Simulator(parse_workflow({"steps": steps, "commands": commands, **config})).run()


class ExtensionTests(unittest.TestCase):
    def test_lost_transient_reuses_attempt(self):
        result = run([{"id": "a", "needs": [], "amount": 5, "failures": 1}],
                     [{"op": "start", "run": "r"}, {"op": "tick", "crash_at": "after_call"},
                      {"op": "restart"}, {"op": "tick"}], max_attempts=1)
        self.assertEqual([c["outcome"] for c in result["final"]["calls"]], ["transient", "applied"])
        self.assertEqual(result["final"]["runs"][0]["steps"][0]["attempts"], 1)

    def test_repeated_cancel_of_cancelling_run_is_harmless(self):
        result = run([{"id": "a", "needs": [], "amount": 5}],
                     [{"op": "start", "run": "r"}, {"op": "tick", "crash_at": "after_begin"},
                      {"op": "restart"}, {"op": "cancel", "run": "r"}, {"op": "cancel", "run": "r"},
                      {"op": "observe"}, {"op": "tick"}])
        self.assertEqual(result["observations"][0]["runs"][0]["status"], "cancelling")
        self.assertEqual(result["final"]["runs"][0]["status"], "cancelled")
        self.assertEqual(result["final"]["calls"], [
            {"kind": "lookup", "key": ["r", "a"], "attempt": 1, "outcome": "missing"}])

    def test_errors(self):
        for commands, code in [
            ([{"op": "crash"}, {"op": "tick", "crash_at": "after_call"}], "PROCESS_DOWN"),
            ([{"op": "crash"}, {"op": "crash"}], "PROCESS_DOWN"),
            ([{"op": "restart"}], "PROCESS_UP"),
            ([{"op": "cancel", "run": "nope"}], "UNKNOWN_RUN"),
        ]:
            with self.assertRaises(DomainError) as ctx:
                run([{"id": "a", "needs": [], "amount": 5}], commands)
            self.assertEqual(ctx.exception.code, code)


if __name__ == "__main__":
    unittest.main()
