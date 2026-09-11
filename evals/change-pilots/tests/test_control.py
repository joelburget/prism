import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from experiments.control import make_plan, grade_case, grade_submission, source_patch, tree_fingerprint, execute_plan
from experiments.costs import estimate, select_models
from run import load_cases


class ControlTests(unittest.TestCase):
    def test_cost_matrix_and_cumulative_input(self):
        result = estimate(select_models(), repetitions=3)
        self.assertEqual(result["runs"], 216)
        self.assertAlmostEqual(result["total_usd"]["planning"], 834.624)
        self.assertEqual(len(result["models"]), 8)
        for row in result["models"]:
            self.assertEqual(row["runs"], 27)
        with self.assertRaises(ValueError):
            estimate(select_models(), repetitions=0)
        with self.assertRaises(ValueError):
            select_models(["luna", "luna"])

    def test_plan_contains_frozen_independent_cells_without_model_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = make_plan(Path(tmp) / "results", ["luna", "haiku"],
                             ["ledger-refunds"], ["prism", "python"], repetitions=2)
            self.assertEqual(len(plan["runs"]), 8)
            self.assertEqual(len({r["run_id"] for r in plan["runs"]}), 8)
            self.assertTrue(all(r["starter_sha256"] for r in plan["runs"]))
            self.assertTrue(all("OPENAI_API_KEY" not in r["prompt"] for r in plan["runs"]))
            self.assertEqual(list((Path(tmp) / "results/runs").iterdir()), [])
            with self.assertRaises(ValueError):
                make_plan(Path(tmp) / "results")

    def test_grading_rejects_protocol_failures_and_uses_external_oracle(self):
        case = load_cases(tasks=["ledger-refunds"])[0]
        good = {"exit_code": 0, "stdout": json.dumps(case.expect), "elapsed_seconds": .1}
        self.assertEqual(grade_case(case, good)["status"], "pass")
        for changed in ({"timed_out": True}, {"output_limited": True},
                        {"stdout_invalid_utf8": True}, {"exit_code": 1},
                        {"stdout": json.dumps(case.expect) + "\n{}"},
                        {"stdout": '{"ok":true,"result":{},"extra":1}'}):
            self.assertEqual(grade_case(case, {**good, **changed})["status"], "fail")

    def test_patch_and_fingerprint_record_mode_only_and_added_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            before, after = Path(tmp) / "before", Path(tmp) / "after"
            before.mkdir(); after.mkdir()
            for d in (before, after):
                (d / "run.sh").write_text("echo hello\n")
                (d / "run.sh").chmod(0o644)
            self.assertEqual(tree_fingerprint(before), tree_fingerprint(after))
            (after / "run.sh").chmod(0o755)
            (after / "new.py").write_text("print('new')\n")
            self.assertNotEqual(tree_fingerprint(before), tree_fingerprint(after))
            diff = source_patch(before, after)
            self.assertIn("Executable flag run.sh: False -> True", diff)
            self.assertIn("+print('new')", diff)

    def test_spending_cap_required_before_execution(self):
        for cap in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                execute_plan("/unused", cap)

    def test_compile_failure_is_a_scored_submission_failure(self):
        from experiments.sandbox import SubmissionBuildError
        class FailedBuild:
            def __init__(self, *args, **kwargs):
                pass
            def __enter__(self):
                raise SubmissionBuildError({"exit_code": 1, "stderr": "syntax error"})
            def __exit__(self, *args):
                pass
        scores = grade_submission("fake", Path("unused"), "query-null", "build.sh", FailedBuild)
        for corpus in scores.values():
            self.assertFalse(corpus["passed"])
            self.assertTrue(corpus["build_failed"])
            self.assertTrue(all(case["status"] == "not_run" for case in corpus["cases"]))


if __name__ == "__main__":
    unittest.main()
