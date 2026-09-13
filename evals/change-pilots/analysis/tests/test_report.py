import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from report import code_metrics, group, render

class ReportingTests(unittest.TestCase):
    def test_churn_handles_added_removed_and_changed_files(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);a=root/'a';b=root/'b';a.mkdir();b.mkdir()
            (a/'main.py').write_text('same\nold\n');(b/'main.py').write_text('same\nnew\nmore\n')
            (a/'gone.ts').write_text('gone\n');(b/'test.py').write_text('test\n');(b/'README.md').write_text('not code\n')
            self.assertEqual(code_metrics(a,b),dict(code_lines=4,baseline_lines=3,added_lines=3,removed_lines=2))
            self.assertIsNone(code_metrics(a,root/'missing')['code_lines'])
    def test_failed_and_flagged_rows_stay_in_pass_denominator(self):
        rows=[dict(model='x',checkpoint=2,passed=passed,seconds=seconds,flags=flags,tool_calls=3,code_lines=100,performance=performance)
              for passed,seconds,flags,performance in [(True,60,[],{'screening_passed':True}),(False,180,['timeout'],{'screening_passed':None})]]
        g=group(rows,['model','checkpoint'])[0]
        self.assertEqual((g['passed'],g['n'],g['flagged']),(1,2,1));self.assertEqual(g['median_minutes'],2);self.assertEqual(g['unflagged_median_minutes'],1)
        self.assertEqual((g['performance_passed'],g['performance_measured']),(1,1))
    def test_embedded_data_cannot_close_script_tag(self):
        with tempfile.TemporaryDirectory() as d:
            render({'runs':[],'notes':['</script><script>bad</script>']},Path(d))
            html=(Path(d)/'index.html').read_text();self.assertNotIn('</script><script>bad',html);self.assertIn('\\u003c/script>',html)
if __name__=='__main__':unittest.main()
