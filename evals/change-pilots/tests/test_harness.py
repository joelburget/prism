"""Tests of the evaluation machinery, not implementations of the pilot tasks."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "run.py"
SPEC = importlib.util.spec_from_file_location("change_pilot_runner", MODULE_PATH)
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)


def command(code):
    return [sys.executable, "-c", code]


def example_case(**updates):
    fields = dict(task="query-null", id="example", phase="baseline", description="Harness example",
                  input={"example": 3}, expect={"ok": True, "result": {"value": 3}})
    fields.update(updates)
    return runner.Case(**fields)


class JsonTests(unittest.TestCase):
    def test_duplicate_key_at_any_depth_rejected(self):
        with self.assertRaises(runner.ContractError):
            runner.parse_json('{"ok":true,"result":{"x":1,"x":2}}')

    def test_ambiguous_or_non_integer_numbers_rejected(self):
        for text in ("NaN", "Infinity", "-Infinity", "1.0", "1e0"):
            with self.subTest(text=text), self.assertRaises(runner.ContractError):
                runner.parse_json(text)

    def test_large_integers_and_unicode_preserved(self):
        self.assertEqual(runner.parse_json('[123456789012345678901234567890,"☃",null]'),
                         [123456789012345678901234567890, "☃", None])

    def test_extra_output_rejected(self):
        for text in ('{}\n{}', '{}\ndebug', 'debug\n{}'):
            with self.subTest(text=text), self.assertRaises(runner.ContractError):
                runner.parse_json(text)

    def test_response_envelope_strict(self):
        for value in ({"ok": 1, "result": []}, {"ok": True},
                      {"ok": False, "error": {"code": "X", "message": "ignored?"}},
                      {"ok": True, "result": [], "extra": 1}, []):
            with self.subTest(value=value), self.assertRaises(runner.ContractError):
                runner.response_contract(value)
        runner.response_contract({"ok": False, "error": {"code": "INVALID_INPUT"}})

    def test_comparison_distinguishes_booleans_and_integers(self):
        self.assertIsNotNone(runner.first_difference({"a": [1]}, {"a": [True]}))
        self.assertIsNotNone(runner.first_difference(1, 1.0))

    def test_object_order_ignored_array_order_preserved(self):
        self.assertIsNone(runner.first_difference({"a": 1, "b": 2}, {"b": 2, "a": 1}))
        self.assertIsNotNone(runner.first_difference([1, 2], [2, 1]))

    def test_difference_reports_nested_path(self):
        self.assertIn('$["rows"][0][1]', runner.first_difference({"rows": [[1, 2]]}, {"rows": [[1, 9]]}))

    def test_missing_extra_keys_and_list_length(self):
        self.assertIn("missing keys", runner.first_difference({"x": 1}, {"y": 1}))
        self.assertIn("expected 1 items", runner.first_difference([1], []))


class CorpusTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for task in runner.TASKS:
            directory = self.root / task
            directory.mkdir()
            self.save(task, {"schema_version": 1, "task": task, "cases": [
                {"id": phase, "phase": phase, "description": phase,
                 "input": {}, "expect": {"ok": True, "result": []}}
                for phase in runner.PHASES
            ]})

    def save(self, task, doc):
        (self.root / task / "cases.json").write_text(json.dumps(doc), encoding="utf-8")

    def mutate(self, change):
        task = runner.TASKS[0]
        path = self.root / task / "cases.json"
        doc = json.loads(path.read_text())
        change(doc)
        self.save(task, doc)

    def test_load_and_request_envelope(self):
        cases = runner.load_cases(self.root)
        self.assertEqual(len(cases), 6)
        self.assertEqual(cases[0].request, {"protocol_version": 1, "task": "query-null", "input": {}})

    def test_duplicate_id_rejected(self):
        self.mutate(lambda doc: doc["cases"][1].update(id="baseline"))
        with self.assertRaisesRegex(runner.ContractError, "duplicate id"):
            runner.load_cases(self.root)

    def test_missing_extension_rejected(self):
        self.mutate(lambda doc: doc["cases"].pop())
        with self.assertRaisesRegex(runner.ContractError, "baseline and extension"):
            runner.load_cases(self.root)

    def test_unknown_field_rejected(self):
        self.mutate(lambda doc: doc["cases"][0].update(expected={}))
        with self.assertRaisesRegex(runner.ContractError, "expected exactly"):
            runner.load_cases(self.root)

    def test_boolean_schema_version_rejected(self):
        self.mutate(lambda doc: doc.update(schema_version=True))
        with self.assertRaisesRegex(runner.ContractError, "schema_version"):
            runner.load_cases(self.root)

    def test_selected_task_does_not_require_other_task_files(self):
        (self.root / "workflow-recovery" / "cases.json").unlink()
        (self.root / "ledger-refunds" / "cases.json").unlink()
        self.assertEqual(len(runner.load_cases(self.root, ["query-null"])), 2)

    def test_cli_uses_explicit_corpus_instead_of_public_default(self):
        self.mutate(lambda doc: doc["cases"][0].update(description="private corpus sentinel"))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = runner.main(["list", "--task", "query-null", "--corpus-root", str(self.root)])
        self.assertEqual(status, 0)
        self.assertIn("private corpus sentinel", output.getvalue())
        self.assertNotIn("workflow-recovery", output.getvalue())

    def test_missing_explicit_corpus_does_not_fall_back_to_public(self):
        with contextlib.redirect_stderr(io.StringIO()):
            status = runner.main(["validate", "--corpus-root", str(self.root / 'missing')])
        self.assertEqual(status, 2)

    def test_fingerprint_covers_expected_values_and_ignores_object_key_order(self):
        first = example_case(input={"a": 1, "b": 2})
        reordered = example_case(input={"b": 2, "a": 1})
        changed = example_case(input={"a": 1, "b": 2}, expect={"ok": True, "result": 4})
        self.assertEqual(runner.corpus_digest([first]), runner.corpus_digest([reordered]))
        self.assertNotEqual(runner.corpus_digest([first]), runner.corpus_digest([changed]))


@unittest.skipUnless(os.name == "posix", "runner targets macOS and Linux")
class ProcessTests(unittest.TestCase):
    def invoke(self, code, **kwargs):
        return runner.invoke(command(code), example_case().request, kwargs.pop("timeout", 3), **kwargs)

    def test_request_delivery_and_stderr_diagnostics(self):
        result = self.invoke(
            'import json,sys; r=json.load(sys.stdin); print("diagnostic",file=sys.stderr); '
            'print(json.dumps({"ok":True,"result":{"task":r["task"],"value":r["input"]["example"]}}))'
        )
        self.assertEqual(result["status"], "received")
        self.assertEqual(result["actual"]["result"], {"task": "query-null", "value": 3})
        self.assertIn("diagnostic", result["stderr"])

    def test_domain_error_is_valid_response(self):
        result = self.invoke('print(\'{"ok":false,"error":{"code":"NO_SUCH_INVOICE"}}\')')
        self.assertEqual(result["status"], "received")

    def test_nonzero_exit_fails_even_with_correct_output(self):
        result = self.invoke('import sys; print(\'{"ok":true,"result":{"value":3}}\'); sys.exit(7)')
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["exit_code"], 7)

    def test_missing_executable_is_infrastructure_error(self):
        result = runner.invoke(["/nonexistent/change-pilot-executable"], {}, 1)
        self.assertEqual(result["status"], "error")

    def test_timeout_terminates_process(self):
        result = self.invoke('import time; time.sleep(10)', timeout=0.15)
        self.assertEqual(result["status"], "fail")
        self.assertIn("timeout", result["reason"])
        self.assertLess(result["elapsed_seconds"], 3)

    def test_descendant_holding_pipe_does_not_hang_runner(self):
        result = self.invoke(
            'import subprocess,sys; subprocess.Popen([sys.executable,"-c","import time; time.sleep(10)"]); '
            'print(\'{"ok":true,"result":3}\')', timeout=0.2
        )
        self.assertEqual(result["status"], "fail")
        self.assertIn("timeout", result["reason"])

    def test_output_cap_on_each_stream(self):
        for stream in ("stdout", "stderr"):
            with self.subTest(stream=stream):
                result = self.invoke(f'import sys; sys.{stream}.write("x"*100000)', max_output=1024)
                self.assertEqual(result["status"], "fail")
                self.assertIn(f"{stream} exceeded", result["reason"])

    def test_invalid_utf8_rejected(self):
        result = self.invoke('import sys; sys.stdout.buffer.write(bytes([255]))')
        self.assertEqual(result["status"], "fail")
        self.assertIn("invalid response", result["reason"])

    def test_multiple_json_responses_rejected(self):
        result = self.invoke('print(\'{"ok":true,"result":1}\\n{"ok":true,"result":1}\')')
        self.assertEqual(result["status"], "fail")

    def test_large_stdin_and_stdout_no_pipe_deadlock(self):
        request = {"text": "a" * 200000}
        result = runner.invoke(command(
            'import json,sys; r=json.load(sys.stdin); print(json.dumps({"ok":True,"result":len(r["text"])}))'
        ), request, 3)
        self.assertEqual(result["status"], "received")
        self.assertEqual(result["actual"]["result"], 200000)

    def test_case_pass_and_failure(self):
        code = command('print(\'{"ok":true,"result":{"value":3}}\')')
        self.assertEqual(runner.run_case(example_case(), code, 3, None)["status"], "pass")
        case = example_case(expect={"ok": True, "result": {"value": 4}})
        result = runner.run_case(case, code, 3, None)
        self.assertEqual(result["status"], "fail")
        self.assertIn("expected", result)

    def test_cli_failure_code_and_machine_report(self):
        import shlex
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            args = ["run", "--command", shlex.join(command('print(\'{"ok":true,"result":{"value":4}}\')')),
                    "--report", str(report)]
            with patch.object(runner, "load_cases", return_value=[example_case()]), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(runner.main(args), 1)
            data = json.loads(report.read_text())
            self.assertEqual(data["summary"]["query-null/baseline"]["fail"], 1)
            self.assertEqual(data["corpus_sha256"], runner.corpus_digest([example_case()]))

    def test_run_with_external_corpus_reports_its_fingerprint(self):
        import shlex
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "query-null").mkdir()
            cases = [{"id": phase, "phase": phase, "description": "External corpus",
                      "input": {}, "expect": {"ok": True, "result": "external"}}
                     for phase in runner.PHASES]
            (root / "query-null" / "cases.json").write_text(json.dumps(
                {"schema_version": 1, "task": "query-null", "cases": cases}))
            report = root / "report.json"
            with contextlib.redirect_stdout(io.StringIO()):
                status = runner.main(["run", "--task", "query-null", "--corpus-root", str(root),
                                      "--command", shlex.join(command('print(\'{"ok":true,"result":"external"}\')')),
                                      "--report", str(report)])
            self.assertEqual(status, 0)
            data = json.loads(report.read_text())
            self.assertEqual(data["corpus_root"], str(root.resolve()))
            self.assertEqual(data["corpus_sha256"], runner.corpus_digest(runner.load_cases(root, ["query-null"])))

    def test_cli_launch_error_reports_unrun_cases(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            with patch.object(runner, "load_cases", return_value=[example_case(), example_case(id="second")]), contextlib.redirect_stdout(io.StringIO()):
                result = runner.main(["run", "--command", "/nonexistent/pilot", "--report", str(report)])
            self.assertEqual(result, 2)
            group = json.loads(report.read_text())["summary"]["query-null/baseline"]
            self.assertEqual(group["error"], 1)
            self.assertEqual(group["not_run"], 1)

    def test_invalid_case_selection_does_not_succeed_vacuously(self):
        with patch.object(runner, "load_cases", return_value=[example_case()]), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                runner.main(["run", "--command", "anything", "--case", "does-not-exist"])
            self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
