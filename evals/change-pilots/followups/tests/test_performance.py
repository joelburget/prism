"""Check screening policy and exact workload results; real timings stay external."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from followups.evaluator import performance as perf


class PerformanceTests(unittest.TestCase):
    def report(self, times, correct=True):
        return {'correct': correct, 'samples': [{'elapsed_seconds': t} for t in [999]+times]}

    def test_separation_uses_reference_extremes_and_ignores_warmup(self):
        self.assertEqual(perf.threshold(self.report([1, 2, 1]), self.report([8, 9, 10])), 4)
        self.assertIsNone(perf.threshold(self.report([1, 4, 1]), self.report([8, 9, 10])))
        self.assertIsNone(perf.threshold(self.report([1]), self.report([2.25])))
        self.assertIsNone(perf.threshold(self.report([1], False), self.report([100])))

    def test_measure_stops_on_wrong_answer_and_cannot_pass_threshold(self):
        samples=[{'correct': True, 'elapsed_seconds': 99}, {'correct': False, 'elapsed_seconds': .1}]
        with patch.object(perf, 'sample', side_effect=samples) as sample:
            result=perf.measure(None, 'point-updates', 2, 1, 3)
        self.assertFalse(result['correct']); self.assertEqual(sample.call_count, 2)
        self.assertIsNone(perf.threshold(result, self.report([100])))

    def test_both_python_controls_compute_varied_profiles_from_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            for mode in ('incremental', 'recompute'):
                source=perf.prepare_control(Path(temporary)/mode, 'python', mode)
                for profile in perf.PROFILES:
                    for size, updates in [(1, 7), (13, 31)]:
                        request, expected=perf.workload(size, updates, profile)
                        result=subprocess.run([sys.executable, str(source/'main.py'), mode],
                            input=json.dumps(request), text=True, capture_output=True, check=True)
                        self.assertEqual(json.loads(result.stdout), expected)

    def test_controls_and_generated_build_modes_are_fingerprinted(self):
        hashes=perf.controls_fingerprint()
        self.assertTrue(any(k.endswith('main.pr') for k in hashes))
        self.assertTrue(any(k.endswith('main.ts') for k in hashes))
        with tempfile.TemporaryDirectory() as temporary:
            source=perf.prepare_control(Path(temporary)/'slow', 'prism', 'recompute')
            self.assertIn('let incremental : Bool = false', (source/'main.pr').read_text())
            self.assertTrue((source/'build.sh').stat().st_mode & 0o111)


if __name__ == '__main__': unittest.main()
