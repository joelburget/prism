"""Extra traces beyond the public corpus, exercising recovery corner cases."""
import unittest

from workflow import DomainError, Simulator, build, parse_workflow


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


def leased(**request):
    request.setdefault("steps", [{"id": "a", "needs": [], "amount": 10}])
    request.setdefault("commands", [])
    request.setdefault("workers", ["w", "v"])
    return build(request).run()


def leased_code(**request):
    try:
        leased(**request)
    except DomainError as error:
        return error.code
    return None


class LeasedTest(unittest.TestCase):
    def steps_of(self, result, index=0):
        return result["final"]["runs"][index]["steps"]

    def test_worker_errors_are_ordered_before_ticket_errors(self):
        self.assertEqual(leased_code(commands=[{"op": "call", "worker": "x", "ticket": 1}]),
                         "UNKNOWN_WORKER")
        self.assertEqual(leased_code(commands=[{"op": "crash", "worker": "w"},
                                               {"op": "call", "worker": "w", "ticket": 1}]),
                         "WORKER_DOWN")
        self.assertEqual(leased_code(commands=[{"op": "call", "worker": "w", "ticket": 1}]),
                         "UNKNOWN_TICKET")
        self.assertEqual(leased_code(commands=[{"op": "start", "run": "r"},
                                               {"op": "claim", "worker": "w"},
                                               {"op": "renew", "worker": "v", "ticket": 1}]),
                         "WRONG_WORKER")
        self.assertEqual(leased_code(commands=[{"op": "deliver", "ticket": 7}]), "UNKNOWN_TICKET")
        self.assertEqual(leased_code(commands=[{"op": "restart", "worker": "w"}]), "WORKER_UP")

    def test_expired_ticket_does_not_make_its_worker_busy(self):
        result = leased(commands=[{"op": "start", "run": "r"}, {"op": "claim", "worker": "w"},
                                  {"op": "advance", "by": 5}, {"op": "claim", "worker": "w"},
                                  {"op": "renew", "worker": "w", "ticket": 1}])
        self.assertEqual(result["results"][3], {"ticket": 2})
        self.assertEqual(result["results"][4], {"renewed": False})
        self.assertEqual(self.steps_of(result)[0]["attempts"], 1)

    def test_stale_transient_cannot_reschedule_a_reclaimed_step(self):
        result = leased(steps=[{"id": "a", "needs": [], "amount": 10, "failures": 1}],
                        commands=[{"op": "start", "run": "r"}, {"op": "claim", "worker": "w"},
                                  {"op": "call", "worker": "w", "ticket": 1},
                                  {"op": "advance", "by": 5}, {"op": "claim", "worker": "v"},
                                  {"op": "deliver", "ticket": 1},
                                  {"op": "call", "worker": "v", "ticket": 2},
                                  {"op": "deliver", "ticket": 2}])
        self.assertEqual(result["results"][5], {"committed": False})
        self.assertEqual(result["results"][6], {"kind": "execute", "outcome": "applied"})
        step = self.steps_of(result)[0]
        self.assertEqual((step["status"], step["attempts"], step["ready_at"]),
                         ("succeeded", 1, 0))

    def test_repeated_cancellation_keeps_a_fresh_lookup_lease(self):
        result = leased(commands=[{"op": "start", "run": "r"}, {"op": "claim", "worker": "w"},
                                  {"op": "cancel", "run": "r"}, {"op": "claim", "worker": "v"},
                                  {"op": "cancel", "run": "r"},
                                  {"op": "call", "worker": "v", "ticket": 2},
                                  {"op": "deliver", "ticket": 2}])
        self.assertEqual(result["results"][6], {"committed": True})
        self.assertEqual(result["final"]["runs"][0]["status"], "cancelled")
        self.assertEqual(self.steps_of(result)[0]["status"], "cancelled")

    def test_failing_run_blocks_a_missing_lookup_and_keeps_other_effects(self):
        result = leased(steps=[{"id": "a", "needs": [], "amount": 1, "failures": 1},
                               {"id": "b", "needs": [], "amount": 2}],
                        max_attempts=1,
                        commands=[{"op": "start", "run": "r"}, {"op": "claim", "worker": "w"},
                                  {"op": "claim", "worker": "v"},
                                  {"op": "call", "worker": "w", "ticket": 1},
                                  {"op": "deliver", "ticket": 1},
                                  {"op": "claim", "worker": "w"},
                                  {"op": "call", "worker": "w", "ticket": 3},
                                  {"op": "deliver", "ticket": 3}])
        self.assertEqual(result["results"][6], {"kind": "lookup", "outcome": "missing"})
        self.assertEqual(result["final"]["runs"][0]["status"], "failed")
        self.assertEqual([step["status"] for step in self.steps_of(result)], ["failed", "blocked"])
        self.assertEqual(result["final"]["effects"], [])

    def test_delayed_response_cannot_commit_once_its_lease_expired(self):
        result = leased(commands=[{"op": "start", "run": "r"}, {"op": "claim", "worker": "w"},
                                  {"op": "call", "worker": "w", "ticket": 1},
                                  {"op": "crash", "worker": "w"}, {"op": "deliver", "ticket": 1},
                                  {"op": "advance", "by": 5}, {"op": "restart", "worker": "w"},
                                  {"op": "deliver", "ticket": 1}])
        self.assertEqual(result["results"][4], {"committed": False})
        self.assertEqual(result["results"][7], {"committed": False})
        self.assertEqual(self.steps_of(result)[0]["status"], "running")
        self.assertEqual(len(result["final"]["effects"]), 1)

    def test_observations_are_immutable_snapshots(self):
        result = leased(commands=[{"op": "start", "run": "r"}, {"op": "observe"},
                                  {"op": "claim", "worker": "w"},
                                  {"op": "call", "worker": "w", "ticket": 1},
                                  {"op": "deliver", "ticket": 1}, {"op": "observe"}])
        self.assertEqual(result["results"][1]["runs"][0]["steps"][0],
                         {"id": "a", "status": "pending", "attempts": 0, "ready_at": 0,
                          "lease": None})
        self.assertEqual(result["results"][5]["runs"][0]["status"], "succeeded")

    def test_leased_input_validation(self):
        self.assertEqual(leased_code(commands=[{"op": "tick"}]), "INVALID_INPUT")
        self.assertEqual(leased_code(commands=[{"op": "claim", "worker": "w", "ticket": 1}]),
                         "INVALID_INPUT")
        self.assertEqual(leased_code(lease_duration=0), "INVALID_INPUT")
        self.assertEqual(leased_code(lease_duration=True), "INVALID_INPUT")
        self.assertEqual(leased_code(commands=[{"op": "observe"}] * 2001), "INVALID_INPUT")
        self.assertEqual(leased_code(commands=[{"op": "deliver", "ticket": 0}]), "INVALID_INPUT")
        self.assertEqual(leased_code(steps=[{"id": "a", "needs": ["a"], "amount": 1}]),
                         "DEPENDENCY_CYCLE")

    def test_claim_respects_the_time_bound(self):
        overflow = [{"op": "advance", "by": 2_147_483_647}, {"op": "start", "run": "r"},
                    {"op": "claim", "worker": "w"}]
        self.assertEqual(leased_code(commands=overflow), "TIME_OVERFLOW")
