"""Offline controller integration: fake model/container, real plans and run records."""
from contextlib import ExitStack, redirect_stdout
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experiments import control
from experiments.results import ResultStore


class ControllerIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "results"
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.dict("os.environ", {"OPENAI_API_KEY": "FAKE-NOT-A-CREDENTIAL"}))
        # No private case contents are read, even during plan fingerprinting.
        self.stack.enter_context(patch.object(control, "inputs_fingerprint", return_value={"test-input": "frozen"}))
        self.stack.enter_context(patch.object(control, "_image_id", return_value="sha256:fixed-test-image"))
        self.stack.enter_context(patch.object(control, "load_starter", return_value=SimpleNamespace(digest=lambda: "starter-digest", build=None)))
        self.export_calls = []

        def export(task, output, language):
            self.export_calls.append((task, language))
            (output / "starter").mkdir(parents=True)
            (output / "starter" / "main.py").write_text("print('baseline')\n")
            (output / "starter" / "run.sh").write_text("#!/bin/sh\nexec python3 main.py\n")
            (output / task).mkdir()
            (output / task / "PROBLEM.md").write_text("Implement the public extension.")
            (output / "MANIFEST.json").write_text(json.dumps({"task": task, "language": language}))
            return output

        self.stack.enter_context(patch.object(control, "export_public", side_effect=export))
        instances = self.instances = []

        class FakeSandbox:
            def __init__(self, image, bundle):
                self.image, self.bundle = image, bundle
                self.frozen = False
                instances.append(self)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def execute(self, command, timeout_seconds=60):
                return {"stdout": "test", "exit_code": 0}

            def freeze(self):
                self.frozen = True

            def snapshot(self, destination):
                if not self.frozen:
                    raise AssertionError("submission must be frozen before export")
                shutil.copytree(self.bundle / "starter", destination)
                (destination / "main.py").write_text("print('extension')\n")
                return destination

        self.stack.enter_context(patch("experiments.sandbox.DockerSandbox", FakeSandbox))
        self.agent_result = {"stop_reason": "completed", "estimated_cost_usd": 1.0,
                             "observed_cost_usd": 1.0, "billing_unknown": False,
                             "elapsed_seconds": 1.25, "usage": {"input_tokens": 25, "output_tokens": 10}}

        def agent(config, prompt, execute, emit):
            emit({"type": "fake_agent_request"})
            return dict(self.agent_result)

        self.agent = self.stack.enter_context(patch("experiments.agent.run_agent", side_effect=agent))
        self.scores = {corpus: {"passed": True, "corpus_sha256": "fake-" + corpus,
                              "phases": {"baseline": {"passed": 2, "total": 2},
                                         "extension": {"passed": 1, "total": 1}}, "cases": []}
                       for corpus in ("public", "heldout")}
        self.grader = self.stack.enter_context(patch.object(control, "grade_submission", return_value=self.scores))

    def plan(self, repetitions=3):
        return control.make_plan(self.root, model_keys=["luna"], tasks=["query-null"],
                                 languages=["python"], repetitions=repetitions, per_run_usd=4)

    def test_run_artifacts_score_and_resume_without_extra_model_calls(self):
        plan = self.plan(repetitions=2)
        first = control.execute_plan(self.root, total_budget_usd=10, limit=1)
        self.assertEqual(first, {"runs_finished": 1, "estimated_cost_usd": 1.0})
        run_id = plan["runs"][0]["run_id"]
        store = ResultStore(self.root)
        directory = store.run_dir(run_id)
        result = json.loads((directory / "result.json").read_text())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["scores"], self.scores)
        self.assertTrue(result["success"])
        self.assertIn("+print('extension')", (directory / "source.patch").read_text())
        self.assertIn("fake_agent_request", (directory / "events.jsonl").read_text())
        self.assertTrue((directory / "input-manifest.json").is_file())
        self.assertNotIn("FAKE-NOT-A-CREDENTIAL", (directory / "metadata.json").read_text())
        self.assertNotIn("scores", store.get_run(run_id))
        self.assertTrue(self.instances[0].frozen)
        self.assertEqual(self.instances[0].image, "sha256:fixed-test-image")
        # Grading consumes the frozen snapshot, after the sandbox stopped.
        self.assertEqual(self.grader.call_args.args[1], directory / "source")
        result_bytes = (directory / "result.json").read_bytes()
        resumed = control.execute_plan(self.root, total_budget_usd=10)
        self.assertEqual(resumed, {"runs_finished": 1, "estimated_cost_usd": 2.0})
        self.assertEqual(self.agent.call_count, 2)
        self.assertEqual(control.execute_plan(self.root, total_budget_usd=10)["runs_finished"], 0)
        self.assertEqual(self.agent.call_count, 2)
        self.assertEqual((directory / "result.json").read_bytes(), result_bytes)

    def test_whole_run_reservation_and_resume_include_prior_spend(self):
        self.plan()
        stopped = control.execute_plan(self.root, total_budget_usd=3.99)
        self.assertEqual(stopped["runs_finished"], 0)
        self.agent.assert_not_called()
        self.assertFalse(self.instances)
        one = control.execute_plan(self.root, total_budget_usd=4.5)
        self.assertEqual(one["runs_finished"], 1)
        self.assertEqual(self.agent.call_count, 1)
        still_stopped = control.execute_plan(self.root, total_budget_usd=4.5)
        self.assertEqual(still_stopped["runs_finished"], 0)
        self.assertEqual(self.agent.call_count, 1)
        rest = control.execute_plan(self.root, total_budget_usd=6)
        self.assertEqual(rest, {"runs_finished": 2, "estimated_cost_usd": 3.0})
        self.assertEqual(self.agent.call_count, 3)

    def test_unknown_billing_stops_and_resume_never_retries(self):
        plan = self.plan()
        self.agent_result.update(stop_reason="provider_error", billing_unknown=True,
                                 estimated_cost_usd=3.5, unreconciled_reservation_usd=2.5)
        stopped = control.execute_plan(self.root, total_budget_usd=20)
        self.assertEqual(stopped["runs_finished"], 1)
        self.assertEqual(self.agent.call_count, 1)
        result = ResultStore(self.root).get_run(plan["runs"][0]["run_id"], blind=False)["result"]
        self.assertTrue(result["billing_uncertain"])
        self.assertEqual(result["status"], "infrastructure_error")
        with self.assertRaisesRegex(ValueError, "uncertain billing"):
            control.execute_plan(self.root, total_budget_usd=20)
        self.assertEqual(self.agent.call_count, 1)

    def test_interrupted_run_without_result_blocks_all_new_calls(self):
        plan = self.plan()
        cell = plan["runs"][0]
        ResultStore(self.root).create_run(cell["run_id"], cell)
        with self.assertRaisesRegex(ValueError, "unknown billing"):
            control.execute_plan(self.root, total_budget_usd=20)
        self.agent.assert_not_called()
        self.assertFalse(self.instances)

    def test_exception_after_agent_retains_spend_and_failure_is_not_retried(self):
        plan = self.plan(repetitions=1)
        self.grader.side_effect = RuntimeError("SECRET-EXCEPTION-BODY")
        result = control.execute_plan(self.root, total_budget_usd=20)
        self.assertEqual(result["estimated_cost_usd"], 1.0)
        stored = ResultStore(self.root).get_run(plan["runs"][0]["run_id"], blind=False)["result"]
        self.assertEqual(stored["status"], "infrastructure_error")
        self.assertFalse(stored["billing_uncertain"])
        self.assertNotIn("SECRET-EXCEPTION-BODY", json.dumps(stored))
        self.assertEqual(control.execute_plan(self.root, total_budget_usd=20)["runs_finished"], 0)
        self.assertEqual(self.agent.call_count, 1)

    def test_exception_during_agent_marks_billing_unknown(self):
        self.plan()
        self.agent.side_effect = RuntimeError("SECRET-RESPONSE-BODY")
        result = control.execute_plan(self.root, total_budget_usd=20)
        self.assertEqual(result["runs_finished"], 1)
        with self.assertRaisesRegex(ValueError, "uncertain billing"):
            control.execute_plan(self.root, total_budget_usd=20)
        self.assertEqual(self.agent.call_count, 1)


if __name__ == "__main__":
    unittest.main()
