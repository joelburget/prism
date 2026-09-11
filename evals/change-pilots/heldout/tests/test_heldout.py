"""Evaluator-only maintenance checks; excluded from public agent exports."""

import importlib.util
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
import test_corpus as public_checks
from test_harness import runner


class HeldoutConsistencyTests(public_checks.CorpusConsistencyTests):
    @classmethod
    def setUpClass(cls):
        cls.cases = runner.load_cases(ROOT / "heldout")

    def test_no_case_ids_or_inputs_are_copied_from_public(self):
        public = runner.load_cases(ROOT)
        public_ids = {(case.task, case.id) for case in public}
        public_inputs = {(case.task, json.dumps(case.input, sort_keys=True)) for case in public}
        seen_inputs = set()
        for case in self.cases:
            with self.subTest(case=case.name):
                self.assertTrue(case.id.startswith('heldout-'))
                self.assertNotIn((case.task, case.id), public_ids)
                request = (case.task, json.dumps(case.input, sort_keys=True))
                self.assertNotIn(request, public_inputs)
                self.assertNotIn(request, seen_inputs)
                seen_inputs.add(request)

    def test_each_case_has_evaluator_coverage_notes(self):
        for task in runner.TASKS:
            notes = (ROOT / 'heldout' / task / 'COVERAGE.md').read_text()
            for case in self.cases:
                if case.task == task:
                    with self.subTest(case=case.name):
                        self.assertIn(f'`{case.id}`', notes)

    def test_frozen_manifest_matches(self):
        spec = importlib.util.spec_from_file_location('pilot_freeze', ROOT / 'heldout' / 'freeze.py')
        freeze = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(freeze)
        expected = json.loads((ROOT / 'heldout' / 'MANIFEST.json').read_text())
        self.assertIsNone(runner.first_difference(expected, freeze.snapshot()))


if __name__ == '__main__':
    unittest.main()
