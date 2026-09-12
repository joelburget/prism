"""Native batch integration with real frozen records and fake client/sandbox."""
from contextlib import ExitStack, redirect_stdout
import fcntl
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
from experiments import native_batch as batch
from experiments.results import ResultStore


class NativeBatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "results"
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.object(batch, "inputs_fingerprint", return_value={"test": "frozen"}))
        self.stack.enter_context(patch.object(batch, "_image_id", return_value="sha256:test"))
        self.verification = json.loads(batch.VERIFICATION.read_text())
        self.check = self.stack.enter_context(patch.object(batch, "checked_verification", return_value=self.verification))
        self.stack.enter_context(patch.object(batch, "load_starter", return_value=SimpleNamespace(digest=lambda: "starter", build=None)))
        self.exports = []
        def export(task, output, language):
            self.exports.append((task, language))
            (output / "starter").mkdir(parents=True)
            (output / "starter" / "main.py").write_text("print('baseline')\n")
            (output / task).mkdir()
            (output / task / "PROBLEM.md").write_text("Public extension")
            (output / "MANIFEST.json").write_text(json.dumps({"task": task, "language": language}))
            return output
        self.stack.enter_context(patch.object(batch, "export_public", side_effect=export))
        self.instances = instances = []
        class Sandbox:
            def __init__(self, image, bundle):
                self.bundle, self.frozen = bundle, False
                instances.append(self)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def execute(self, command, timeout_seconds):
                return {"exit_code": 0, "stdout": "canary"}
            def freeze(self):
                self.frozen = True
            def snapshot(self, destination):
                assert self.frozen
                shutil.copytree(self.bundle / "starter", destination)
                (destination / "main.py").write_text("print('extension')\n")
                return destination
        self.stack.enter_context(patch.object(batch, "DockerSandbox", Sandbox))
        class Client:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
        self.clients = self.stack.enter_context(patch.object(batch, "create_client", side_effect=lambda *a: Client()))
        self.stack.enter_context(patch.object(batch, "inspect_boundary", return_value={"passed": True}))
        self.auth = self.stack.enter_context(patch.object(batch, "subscription_status", return_value={"authenticated": True, "method": "chatgpt"}))
        self.agent_result = {"stop_reason": "completed", "tool_calls": 1, "elapsed_seconds": 1.2, "usage": {"input_tokens": 3}}
        self.events = []
        def agent(client, argv, prompt, execute, record, **limits):
            for event in self.events:
                record(event)
            execute("printf canary", 5)
            return dict(self.agent_result)
        self.agent = self.stack.enter_context(patch.object(batch, "run_client", side_effect=agent))
        self.scores = {name: {"passed": True, "phases": {"baseline": {"passed": 1, "total": 1}, "extension": {"passed": 1, "total": 1}}} for name in ("public", "heldout")}
        self.grader = self.stack.enter_context(patch.object(batch, "grade_submission", return_value=self.scores))

    def plan(self, repetitions=2):
        return batch.make_plan(self.root, ["luna"], languages=["python"], repetitions=repetitions)

    def result(self, plan):
        return ResultStore(self.root).get_run(plan["runs"][0]["run_id"], blind=False)["result"]

    def test_default_matrix_has24_independent_cells_and_no_api_cap(self):
        plan = batch.make_plan(self.root)
        self.assertEqual(len(plan["runs"]), 24)
        self.assertEqual(len({c["run_id"] for c in plan["runs"]}), 24)
        self.assertEqual({c["task"] for c in plan["runs"]}, {"ledger-refunds"})
        self.assertEqual(plan["harness"], "subscription-native-v2")
        self.assertFalse(plan["accounting"]["api_fallback"])
        self.assertIsNone(plan["accounting"]["enforceable_dollar_cap"])
        self.agent.assert_not_called()

    def test_prompt_describes_writable_remote_workspace_and_installed_editor(self):
        prompt = batch.native_prompt_for("ledger-refunds", "typescript")
        self.assertIn("/work and /work/starter are writable", prompt)
        self.assertIn("apply_patch command is installed", prompt)

    def test_operator_stop_marker_starts_no_new_cell(self):
        self.plan()
        (self.root / "STOP_AFTER_CURRENT").touch()
        result = batch.execute_plan(self.root)
        self.assertTrue(result["paused_by_operator"])
        self.agent.assert_not_called()
        self.assertEqual(list((self.root / "runs").iterdir()), [])

    def test_run_freezes_grades_preserves_review_artifacts_and_resumes(self):
        plan = self.plan()
        self.assertEqual(batch.execute_plan(self.root, 1)["runs_finished"], 1)
        first = self.result(plan)
        self.assertEqual(first["status"], "completed")
        self.assertEqual(first["server_model_identity"], "unverified")
        self.assertIsNone(first["estimated_cost_usd"])
        directory = ResultStore(self.root).run_dir(plan["runs"][0]["run_id"])
        for artifact in ("baseline/main.py", "source/main.py", "source.patch", "native-argv.json", "native-environment.json", "native-result.json", "problem.md", "input-manifest.json"):
            self.assertTrue((directory / artifact).is_file(), artifact)
        self.assertTrue(self.instances[0].frozen)
        self.assertEqual(self.grader.call_args.args[1], directory / "source")
        self.assertEqual(self.exports, [("ledger-refunds", "python")])
        original = (directory / "result.json").read_bytes()
        self.assertEqual(batch.execute_plan(self.root)["runs_finished"], 1)
        self.assertEqual(batch.execute_plan(self.root)["runs_finished"], 0)
        self.assertEqual(self.agent.call_count, 2)
        self.assertEqual((directory / "result.json").read_bytes(), original)
        self.assertNotIn("scores", ResultStore(self.root).get_run(plan["runs"][0]["run_id"]))

    def test_quota_or_client_failure_stops_not_ability_zero_and_no_retry(self):
        plan = self.plan()
        self.agent_result["stop_reason"] = "native_client_error"
        self.assertTrue(batch.execute_plan(self.root)["stopped_on_infrastructure_run"])
        result = self.result(plan)
        self.assertEqual(result["status"], "infrastructure_error")
        self.assertIsNone(result["success"])
        self.assertNotIn("scores", result)
        self.grader.assert_not_called()
        self.assertTrue((ResultStore(self.root).run_dir(plan["runs"][0]["run_id"]) / "source.patch").exists())
        with self.assertRaisesRegex(ValueError, "prior infrastructure"):
            batch.execute_plan(self.root)
        self.assertEqual(self.agent.call_count, 1)

    def test_nul_input_then_corrected_command_is_graded_without_poisoning_run(self):
        from experiments.native_bridge import ExecuteBridge
        plan = self.plan(1)
        def agent(client, argv, prompt, execute, record, **limits):
            bridge = ExecuteBridge(execute, record)
            bridge.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize"})
            def call(i, command):
                return bridge.handle({"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {
                    "name": "execute", "arguments": {"command": command, "timeout_seconds": 30}}})
            self.assertTrue(call(1, "printf 'a\0b'")["result"]["isError"])
            self.assertFalse(call(2, r"printf 'a\0b'")["result"]["isError"])
            return dict(self.agent_result)
        self.agent.side_effect = agent
        batch.execute_plan(self.root)
        self.assertEqual(self.result(plan)["status"], "completed")
        self.grader.assert_called_once()

    def test_unexpected_callback_value_error_remains_infrastructure(self):
        plan = self.plan(1)
        def agent(client, argv, prompt, execute, record, **limits):
            with self.assertRaises(ValueError):
                execute("valid command", 30)
            return dict(self.agent_result)
        self.agent.side_effect = agent
        with patch.object(batch.DockerSandbox, "execute", side_effect=ValueError("internal failure")):
            batch.execute_plan(self.root)
        self.assertEqual(self.result(plan)["status"], "infrastructure_error")
        self.grader.assert_not_called()

    def test_auth_failure_does_not_start_model(self):
        plan = self.plan()
        self.auth.return_value = {"authenticated": False}
        batch.execute_plan(self.root)
        self.agent.assert_not_called()
        self.assertEqual(self.result(plan)["status"], "infrastructure_error")

    def test_model_mismatch_stops_without_grading(self):
        plan = self.plan()
        self.events = [{"event": "native_event", "payload": {"type": "assistant", "message": {"model": "other-model"}}}]
        batch.execute_plan(self.root)
        result = self.result(plan)
        self.assertEqual(result["server_model_identity"], "mismatch")
        self.assertEqual(result["status"], "infrastructure_error")
        self.grader.assert_not_called()

    def test_max_turns_is_scored_resource_limit(self):
        plan = self.plan(1)
        self.agent_result["stop_reason"] = "native_client_error"
        self.events = [{"event": "native_event", "payload": {"type": "result", "subtype": "error_max_turns", "is_error": True}}]
        batch.execute_plan(self.root)
        self.assertEqual(self.result(plan)["stop_reason"], "native_turn_limit")
        self.assertEqual(self.result(plan)["status"], "completed")
        self.grader.assert_called_once()

    def test_tool_resource_exhaustion_and_trace_limit_are_scored(self):
        plan = self.plan(1)
        def bounded_agent(client, argv, prompt, execute, record, **limits):
            with self.assertRaises(batch.SandboxFrozenError):
                execute("too much output", 5)
            return {"stop_reason": "native_output_limit"}
        self.agent.side_effect = bounded_agent
        with patch.object(batch.DockerSandbox, "execute", side_effect=batch.SandboxFrozenError("resource exhausted")):
            batch.execute_plan(self.root)
        result = self.result(plan)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["tool_resource_limits"], ["task_frozen_after_resource_limit"])
        self.grader.assert_called_once()

    def test_interrupted_cell_and_parallel_controller_block_before_model_calls(self):
        plan = self.plan()
        with (self.root / "execution.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, "running controller"):
                batch.execute_plan(self.root)
        cell = plan["runs"][0]
        ResultStore(self.root).create_run(cell["run_id"], cell)
        with self.assertRaisesRegex(ValueError, "interrupted"):
            batch.execute_plan(self.root)
        self.agent.assert_not_called()

    def test_stale_inputs_and_verification_block(self):
        self.plan()
        with patch.object(batch, "inputs_fingerprint", return_value={"changed": "yes"}):
            with self.assertRaisesRegex(ValueError, "inputs changed"):
                batch.execute_plan(self.root)
        report = self.root / "NATIVE_VERIFICATION.json"
        report.chmod(0o600)
        report.write_text('{}')
        with self.assertRaisesRegex(ValueError, "verification changed"):
            batch.execute_plan(self.root)
        self.agent.assert_not_called()


class VerificationTests(unittest.TestCase):
    def test_real_verification_hashes_and_model_efforts(self):
        report = json.loads(batch.VERIFICATION.read_text())
        result = batch.checked_verification(batch.VERIFICATION, report["image_id"], batch.select_models(None))
        self.assertEqual(result["image_id"], report["image_id"])
        with self.assertRaisesRegex(ValueError, "image"):
            batch.checked_verification(batch.VERIFICATION, "wrong-image", [])
        with self.assertRaisesRegex(ValueError, "task image"):
            batch.checked_verification(batch.VERIFICATION, report["image_id"], [], "wrong-task-image")
        with patch.object(batch, "file_hash", return_value="changed"):
            with self.assertRaisesRegex(ValueError, "source changed"):
                batch.checked_verification(batch.VERIFICATION, report["image_id"], [])
        model = dict(batch.select_models(["luna"])[0], reasoning="high")
        with self.assertRaisesRegex(ValueError, "model/effort"):
            batch.checked_verification(batch.VERIFICATION, report["image_id"], [model])

    def test_model_metadata_not_model_prose(self):
        self.assertEqual(batch.observed_models({"event": "native_event", "payload": {"type": "result", "result": "I am another-model", "modelUsage": {"claude-opus-5": {}}}}), {"claude-opus-5"})


if __name__ == "__main__":
    unittest.main()
