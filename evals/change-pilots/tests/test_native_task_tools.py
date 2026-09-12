"""No-model checks for the tools available in every native task language.

PRISM_EVAL_DOCKER_TESTS=1 enables real patch operations and ledger baselines.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from experiments.native_bridge import ExecuteBridge
from experiments.native_task_setup import DEFAULT_IMAGE, prepare_context
from experiments.sandbox import DockerEvaluation, DockerSandbox
from export_public import export_public
from run import first_difference, load_cases, parse_json, response_contract
from starter_support import LANGUAGES, load_starter


class ContextTests(unittest.TestCase):
    def test_context_contains_only_dockerfile(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = Path(temporary) / "context"
            prepare_context(context)
            self.assertEqual([p.name for p in context.iterdir()], ["NativeTaskDockerfile"])
            with self.assertRaises(FileExistsError):
                prepare_context(context)


@unittest.skipUnless(os.environ.get("PRISM_EVAL_DOCKER_TESTS") == "1", "opt-in Docker tests")
class NativeTaskIntegrationTests(unittest.TestCase):
    image = os.environ.get("PRISM_EVAL_NATIVE_TASK_IMAGE", DEFAULT_IMAGE)

    def test_real_patch_stdin_add_update_delete_and_versions(self):
        with tempfile.TemporaryDirectory() as temporary:
            with DockerSandbox(self.image, Path(temporary)) as sandbox:
                for patch_text, check in (
                    ("*** Begin Patch\n*** Add File: canary.txt\n+before\n*** End Patch", "test \"$(cat canary.txt)\" = before"),
                    ("*** Begin Patch\n*** Update File: canary.txt\n@@\n-before\n+after\n*** End Patch", "test \"$(cat canary.txt)\" = after"),
                    ("*** Begin Patch\n*** Delete File: canary.txt\n*** End Patch", "test ! -e canary.txt"),
                ):
                    result = sandbox.execute("apply_patch <<'PATCH'\n" + patch_text + "\nPATCH\n")
                    self.assertEqual(result["exit_code"], 0, result)
                    self.assertEqual(sandbox.execute(check)["exit_code"], 0)
                for command, expected in (("prism --version", "prism 0.18.0"),
                                          ("python3 --version", "Python 3.14.7"),
                                          ("node --version", "v25.2.1"),
                                          ("tsc --version", "Version 5.9.3"),
                                          ("/opt/pilot/codex-patch --version", "codex-cli 0.154.0")):
                    result = sandbox.execute(command)
                    self.assertEqual(result["exit_code"], 0, result)
                    self.assertEqual(result["stdout"].strip(), expected)
                result = sandbox.execute("npx --version && test ! -e /var/run/docker.sock && test ! -e /home/native && test ! -e /Users/joel && test ! -e /evaluator && test ! -e /tmp/home/.codex/auth.json")
                self.assertEqual(result["exit_code"], 0, result)
                self.assertEqual(sandbox.execute("npm --version")["stdout"], result["stdout"])

    def test_nul_rejected_and_corrected_command_runs_in_same_container(self):
        with tempfile.TemporaryDirectory() as temporary:
            with DockerSandbox(self.image, Path(temporary)) as sandbox:
                events = []
                bridge = ExecuteBridge(sandbox.execute, events.append)
                bridge.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize"})
                def call(i, command):
                    return bridge.handle({"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {
                        "name": "execute", "arguments": {"command": command, "timeout_seconds": 30}}})["result"]
                self.assertTrue(call(1, "printf 'a\0b'")["isError"])
                result = call(2, "printf corrected > canary.txt && cat canary.txt")
                self.assertFalse(result["isError"])
                self.assertEqual(json.loads(result["content"][0]["text"])["stdout"], "corrected")
                self.assertFalse(sandbox.frozen)
                self.assertEqual(bridge.calls, 2)

    def test_three_ledger_starter_public_baselines(self):
        cases = [case for case in load_cases(tasks=["ledger-refunds"]) if case.phase == "baseline"]
        self.assertEqual(len(cases), 24)
        for language in LANGUAGES:
            with self.subTest(language=language), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                bundle = export_public("ledger-refunds", root / "bundle", language=language)
                starter = load_starter("ledger-refunds", language)
                with DockerSandbox(self.image, bundle) as sandbox:
                    source = sandbox.snapshot(root / "source")
                with DockerEvaluation(self.image, source, build=starter.build) as evaluator:
                    for case in cases:
                        result = evaluator.run_input(json.dumps(case.request).encode() + b"\n")
                        self.assertEqual(result["exit_code"], 0, f"{case.name}: {result}")
                        self.assertFalse(result["timed_out"] or result["output_limited"], result)
                        actual = parse_json(result["stdout"])
                        response_contract(actual)
                        self.assertIsNone(first_difference(case.expect, actual), case.name)


if __name__ == "__main__":
    unittest.main()
