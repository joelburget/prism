import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
spec=importlib.util.spec_from_file_location('checkpoint_export',ROOT/'export_public.py')
exporter=importlib.util.module_from_spec(spec); spec.loader.exec_module(exporter)

class ExportTests(unittest.TestCase):
    def predecessor(self,path,task='query-null',passed=True):
        path.mkdir(); source=path/'source'; source.mkdir()
        (source/'run.sh').write_text('#!/bin/sh\nexec python3 main.py\n'); (source/'run.sh').chmod(0o755)
        (source/'main.py').write_text('print("starter-only-marker")\n')
        fingerprint=exporter.source_files(source)[2]
        (path/'metadata.json').write_text(json.dumps({'task':task,'language':'python','run_id':'private-parent-id','model':'private-model-marker'}))
        scores={}
        for name,base in [('public',ROOT.parent),('heldout',ROOT.parent/'heldout')]:
            cases=exporter.runner.load_cases(base,[task])
            scores[name]={'corpus_sha256':exporter.runner.corpus_digest(cases),'cases':[{'id':c.id,'status':'pass' if passed else 'fail'} for c in cases]}
        (path/'result.json').write_text(json.dumps({'status':'completed','success':passed,'source_sha256':fingerprint,'scores':scores,'private':'private-score-marker'}))
        return path

    def test_tests_only_bundle_runs_without_evaluator_imports(self):
        with tempfile.TemporaryDirectory() as temporary:
            output=Path(temporary)/'bundle'
            self.assertIsNone(exporter.export('query-null',output))
            actual={str(p.relative_to(output)) for p in output.rglob('*') if p.is_file()}
            self.assertEqual(actual,{'run.py','public_runner.py','README.md','MANIFEST.json','query-null/PROBLEM.md','query-null/PREVIOUS.md','query-null/cases.json'})
            result=subprocess.run([sys.executable,'run.py','validate','--task','query-null'],cwd=output,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            manifest=json.loads((output/'MANIFEST.json').read_text())
            for name,sha in manifest['files'].items(): self.assertEqual(exporter.digest((output/name).read_bytes()),sha)

    def test_frozen_source_only_and_evaluator_lineage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); parent=self.predecessor(root/'parent'); output=root/'bundle'
            receipt=exporter.export('query-null',output,parent)
            self.assertTrue(receipt['parent_passed'])
            self.assertEqual(receipt['parent_run_id'],'private-parent-id')
            self.assertTrue((output/'starter'/'run.sh').stat().st_mode & 0o111)
            content='\n'.join(p.read_text() for p in output.rglob('*') if p.is_file())
            for secret in ('private-parent-id','private-model-marker','private-score-marker'):
                self.assertNotIn(secret,content)
            self.assertFalse((output/'evaluator').exists())
            self.assertFalse((output/'workflow-recovery').exists())

    def test_chain_preserves_failed_predecessors_controlled_rejects(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); parent=self.predecessor(root/'parent',passed=False)
            self.assertFalse(exporter.export('query-null',root/'chain',parent)['parent_passed'])
            with self.assertRaisesRegex(ValueError,'controlled baseline'): exporter.export('query-null',root/'controlled',parent,'controlled')

    def test_reject_changed_source_and_mismatched_task(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); parent=self.predecessor(root/'parent')
            with self.assertRaisesRegex(ValueError,'task/language'): exporter.export('workflow-recovery',root/'wrong',parent)
            (parent/'source'/'main.py').write_text('changed')
            with self.assertRaisesRegex(ValueError,'changed since grading'): exporter.export('query-null',root/'changed',parent)

    def test_reject_symlink_and_wrong_corpus(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); parent=self.predecessor(root/'parent')
            path=parent/'result.json'; result=json.loads(path.read_text()); result['scores']['heldout']['corpus_sha256']='wrong'; path.write_text(json.dumps(result))
            with self.assertRaisesRegex(ValueError,'different checkpoint-one corpus'): exporter.export('query-null',root/'wrong',parent)
            (parent/'source'/'leak').symlink_to(parent/'metadata.json')
            with self.assertRaisesRegex(ValueError,'symlinks'): exporter.export('query-null',root/'linked',parent)

    def test_reject_existing_or_repository_destination(self):
        with self.assertRaisesRegex(ValueError,'outside'): exporter.export('query-null',ROOT/'forbidden-export')
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError,'new'): exporter.export('query-null',Path(temporary))

if __name__=='__main__': unittest.main()
