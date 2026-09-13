"""Extra traces beyond the public corpus, exercising recovery corner cases."""
import unittest

from workflow import DomainError, Simulator, parse_workflow


def simulate(**request):
    request.setdefault("steps", [{"id": "a", "needs": [], "amount": 10}])
    request.setdefault("commands", [])
    return Simulator(parse_workflow(request)).run()


def code(**request):
    try:
        simulate(**request)
    except DomainError as error:
        return error.code
    return None


class RecoveryTest(unittest.TestCase):
    def test_crash_while_down_is_rejected(self):
        self.assertEqual(code(commands=[{"op": "crash"}, {"op": "crash"}]), "PROCESS_DOWN")

    def test_duplicate_run_checked_after_availability(self):
        commands = [{"op": "start", "run": "r"}, {"op": "crash"}, {"op": "start", "run": "r"}]
        self.assertEqual(code(commands=commands), "PROCESS_DOWN")

    def test_cancellation_of_final_step_yields_cancelled_run(self):
        result = simulate(commands=[
            {"op": "start", "run": "r"}, {"op": "tick", "crash_at": "after_call"},
            {"op": "restart"}, {"op": "cancel", "run": "r"}, {"op": "tick"}, {"op": "tick"}])
        run = result["final"]["runs"][0]
        self.assertEqual(run["status"], "cancelled")
        self.assertEqual(run["steps"][0]["status"], "succeeded")
        self.assertEqual([call["kind"] for call in result["final"]["calls"]], ["execute", "lookup"])

    def test_repeated_cancel_of_cancelling_run_is_harmless(self):
        result = simulate(commands=[
            {"op": "start", "run": "r"}, {"op": "tick", "crash_at": "after_begin"},
            {"op": "restart"}, {"op": "cancel", "run": "r"}, {"op": "cancel", "run": "r"},
            {"op": "observe"}, {"op": "tick"}])
        self.assertEqual(result["observations"][0]["runs"][0]["status"], "cancelling")
        self.assertEqual(result["final"]["runs"][0]["steps"][0]["status"], "cancelled")
        self.assertEqual(result["final"]["effects"], [])

    def test_cancel_of_failed_run_keeps_flag_false(self):
        result = simulate(
            steps=[{"id": "a", "needs": [], "amount": 10, "failures": 1}],
            commands=[{"op": "start", "run": "r"}, {"op": "tick"}, {"op": "cancel", "run": "r"},
                      {"op": "tick"}],
            max_attempts=1)
        run = result["final"]["runs"][0]
        self.assertEqual((run["status"], run["cancel_requested"]), ("failed", False))

    def test_crash_at_after_call_skips_retry_overflow(self):
        result = simulate(
            steps=[{"id": "a", "needs": [], "amount": 10, "failures": 1}],
            commands=[{"op": "advance", "by": 2_147_483_647}, {"op": "start", "run": "r"},
                      {"op": "tick", "crash_at": "after_call"}])
        self.assertFalse(result["final"]["up"])
        self.assertEqual(result["final"]["runs"][0]["steps"][0]["status"], "running")

    def test_independent_runs_have_separate_failure_counters(self):
        result = simulate(
            steps=[{"id": "a", "needs": [], "amount": 10, "failures": 1}],
            commands=[{"op": "start", "run": "r1"}, {"op": "start", "run": "r2"},
                      {"op": "tick"}, {"op": "tick"}, {"op": "advance", "by": 2},
                      {"op": "tick"}, {"op": "tick"}])
        self.assertEqual([call["outcome"] for call in result["final"]["calls"]],
                         ["transient", "transient", "applied", "applied"])

    def test_null_checkpoint_is_invalid_input(self):
        self.assertEqual(code(commands=[{"op": "tick", "crash_at": None}]), "INVALID_INPUT")

    def test_self_dependency_is_a_cycle(self):
        self.assertEqual(code(steps=[{"id": "a", "needs": ["a"], "amount": 1}]),
                         "DEPENDENCY_CYCLE")


if __name__ == "__main__":
    unittest.main()
