"""Regression traces for concurrent recovery and retained transport responses."""
import unittest

from leased import LeasedSimulator


class LeasedRecoveryTests(unittest.TestCase):
    def simulator(self, steps=None, **config):
        return LeasedSimulator({
            "steps": steps or [{"id": "s", "needs": [], "amount": 7}],
            "workers": ["a", "b"], "commands": [], **config,
        })

    def test_repeated_cancel_preserves_lookup_and_observation(self):
        sim = self.simulator()
        sim.apply({"op": "start", "run": "r"})
        sim.apply({"op": "claim", "worker": "a"})
        before = sim.snapshot()
        sim.apply({"op": "cancel", "run": "r"})
        sim.apply({"op": "claim", "worker": "b"})
        sim.apply({"op": "cancel", "run": "r"})
        self.assertEqual(sim.apply({"op": "call", "worker": "b", "ticket": 2}),
                         {"kind": "lookup", "outcome": "missing"})
        self.assertEqual(sim.apply({"op": "deliver", "ticket": 2}), {"committed": True})
        self.assertEqual(sim.runs[0].status, "cancelled")
        self.assertEqual(before["runs"][0]["steps"][0]["lease"]["expires"], 5)
        self.assertEqual(before["runs"][0]["status"], "active")

    def test_failure_drains_applied_parallel_branch(self):
        sim = self.simulator([
            {"id": "bad", "needs": [], "amount": 1, "failures": 1},
            {"id": "good", "needs": [], "amount": 2},
            {"id": "pending", "needs": ["good"], "amount": 3},
        ], max_attempts=1)
        sim.apply({"op": "start", "run": "r"})
        for worker in ("a", "b"):
            sim.apply({"op": "claim", "worker": worker})
        sim.apply({"op": "call", "worker": "b", "ticket": 2})
        sim.apply({"op": "call", "worker": "a", "ticket": 1})
        sim.apply({"op": "deliver", "ticket": 1})
        self.assertEqual(sim.runs[0].status, "failing")
        sim.apply({"op": "cancel", "run": "r"})
        self.assertFalse(sim.runs[0].cancel_requested)
        self.assertEqual(sim.apply({"op": "deliver", "ticket": 2}), {"committed": False})
        sim.apply({"op": "claim", "worker": "a"})
        sim.apply({"op": "call", "worker": "a", "ticket": 3})
        sim.apply({"op": "deliver", "ticket": 3})
        self.assertEqual(sim.runs[0].status, "failed")
        self.assertEqual([step.status for step in sim.runs[0].steps],
                         ["failed", "succeeded", "blocked"])
        self.assertEqual([call["outcome"] for call in sim.audit],
                         ["applied", "transient", "found"])
        self.assertEqual(len(sim.service.effects), 1)

    def test_response_survives_crash_and_repeated_call_is_cached(self):
        sim = self.simulator()
        sim.apply({"op": "start", "run": "r"})
        sim.apply({"op": "claim", "worker": "a"})
        call = {"op": "call", "worker": "a", "ticket": 1}
        self.assertEqual(sim.apply(call), sim.apply(call))
        sim.apply({"op": "crash", "worker": "a"})
        self.assertEqual(sim.apply({"op": "deliver", "ticket": 1}), {"committed": False})
        sim.apply({"op": "restart", "worker": "a"})
        self.assertEqual(sim.apply({"op": "deliver", "ticket": 1}), {"committed": True})
        self.assertEqual(sim.apply({"op": "deliver", "ticket": 1}), {"committed": False})
        self.assertEqual(len(sim.audit), 1)
        self.assertEqual(sim.runs[0].status, "succeeded")


if __name__ == "__main__":
    unittest.main()
