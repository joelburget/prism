import importlib.util
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'evaluator'))
spec=importlib.util.spec_from_file_location('checkpoint_grade',ROOT/'evaluator'/'grade.py')
grader=importlib.util.module_from_spec(spec); spec.loader.exec_module(grader)

class GradingTests(unittest.TestCase):
    def source(self,root):
        root.mkdir(); (root/'run.sh').write_text('#!/bin/sh\nexit 0\n'); (root/'run.sh').chmod(0o755)
        return root

    def test_strict_response_comparison(self):
        case=grader.runner.Case(task='query-null',id='test',phase='extension',description='test',input={},expect={'ok':True,'result':{'x':1}})
        execution={'exit_code':0,'stdout':'{"ok":true,"result":{"x":1}}'}
        self.assertEqual(grader.grade_case(case,execution)['status'],'pass')
        for stdout in ['{"ok":true,"result":{"x":true}}','{"ok":true,"result":{"x":1},"extra":0}','{"ok":true,"result":{"x":1.0}}','{"ok":true,"result":{"x":1}} trailing']:
            self.assertEqual(grader.grade_case(case,{**execution,'stdout':stdout})['status'],'fail')
        self.assertEqual(grader.grade_case(case,{**execution,'stdout_invalid_utf8':True})['status'],'fail')
        self.assertEqual(grader.grade_case(case,{**execution,'timed_out':True})['status'],'fail')

    def test_full_cumulative_grading_and_hashes(self):
        all_cases=[c for base in (ROOT,ROOT/'evaluator'/'heldout') for c in grader.runner.load_cases(base,['workflow-recovery'])]
        responses={json.dumps(c.request,sort_keys=True):c.expect for c in all_cases}
        calls=[]
        class Evaluator:
            def __init__(self,image,source,build): self.build=build
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def run_input(self,data,timeout_seconds):
                request=json.loads(data); calls.append(request)
                return {'exit_code':0,'stdout':json.dumps(responses[json.dumps(request,sort_keys=True)])}
        with tempfile.TemporaryDirectory() as temporary:
            source=self.source(Path(temporary)/'source')
            result=grader.grade('workflow-recovery','python',source,'sha256:test',factory=Evaluator)
        self.assertTrue(result['success']); self.assertTrue(result['full_corpus_selected'])
        self.assertIsNone(result['algorithmic_qualification'])
        self.assertEqual(len(calls),125)
        self.assertEqual(result['scores']['public']['phases']['baseline'],{'passed':57,'total':57})
        self.assertEqual(result['scores']['heldout']['phases']['extension'],{'passed':31,'total':31})

    def test_partial_grade_is_marked_and_build_failure_is_not_infrastructure(self):
        class BrokenBuild:
            def __init__(self,image,source,build): assert build=='build.sh'
            def __enter__(self): raise grader.SubmissionBuildError({'exit_code':1,'stderr':'compile error'})
            def __exit__(self,*args): pass
        with tempfile.TemporaryDirectory() as temporary:
            source=self.source(Path(temporary)/'source')
            result=grader.grade('query-null','prism',source,'sha256:test',phase='extension',factory=BrokenBuild)
        self.assertFalse(result['success']); self.assertFalse(result['full_corpus_selected'])
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['scores']['heldout']['phases']['extension'],{'passed':0,'total':52})
        self.assertEqual(result['scores']['public']['phases']['baseline']['total'],0)

    def test_source_mutation_invalidates_grade(self):
        class Mutating:
            def __init__(self,image,source,build): self.source=source
            def __enter__(self): return self
            def __exit__(self,*args): (self.source/'extra.py').write_text('changed')
            def run_input(self,*args,**kwargs): return {'exit_code':1}
        with tempfile.TemporaryDirectory() as temporary:
            source=self.source(Path(temporary)/'source')
            with self.assertRaisesRegex(ValueError,'source changed'): grader.grade('query-null','python',source,'sha256:test',factory=Mutating)

    def test_report_cannot_mutate_frozen_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            source=self.source(Path(temporary)/'source')
            result=subprocess.run([sys.executable,str(ROOT/'evaluator'/'grade.py'),'--task','query-null','--language','python','--source',str(source),'--image','unused','--report',str(source/'report.json')],capture_output=True,text=True)
            self.assertEqual(result.returncode,2)
            self.assertIn('outside the frozen source',result.stderr)
            self.assertFalse((source/'report.json').exists())

if __name__=='__main__': unittest.main()
