import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from git_reviews import export
from report import sha
from experiments.control import tree_fingerprint

class GitReviewTests(unittest.TestCase):
    def test_stage_parent_trees_match_frozen_baselines_and_invalid_archive_is_explicit(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);repo=root/'repo';repo.mkdir();results=root/'results';output=root/'report';output.mkdir()
            def git(*a):return subprocess.run(['git','-C',str(repo),*a],capture_output=True,text=True,check=True).stdout.strip()
            git('init');git('config','user.name','Review test');git('config','user.email','test@example.invalid')
            (repo/'README').write_text('base');git('add','.');git('commit','-m','base');original=git('rev-parse','HEAD')
            cells=[];rows=[]
            for model in ['first','second']:
                before='baseline\n'
                for cp in [1,2]:
                    ident=f'{model}-{cp}';d=results/'runs'/ident;(d/'baseline').mkdir(parents=True)
                    (d/'baseline/main.py').write_text(before);(d/'problem.md').write_text(f'checkpoint {cp}')
                    available=not(model=='second' and cp==2)
                    if available:
                        (d/'source').mkdir();(d/'source/main.py').write_text(f'{model}-{cp}\n');(d/'source/main.py').chmod(0o755)
                    if cp==2:
                        # Review baseline artifacts lose modes; predecessor source retains them.
                        (d/'baseline/main.py').chmod(0o400)
                    (d/'result.json').write_text('{}')
                    cells.append(dict(run_id=ident,chain_id=model,checkpoint=cp,task='query-null',language='python',model={'key':model},repetition=1,parent_run_id=f'{model}-1' if cp==2 else None,starter_sha256='fixture'))
                    rows.append(dict(run_id=ident,checkpoint=cp,model=model,model_id=model,task='query-null',language='python',source_available=available,source_sha256=tree_fingerprint(d/'source') if available else None,passed=available,seconds=60,tool_calls=3,flags=[] if available else ['invalid_submission'],result_sha256=sha(d/'result.json')))
                    before=f'{model}-{cp}\n'
            (results/'plan.json').write_text(json.dumps({'runs':cells}));(output/'runs.json').write_text(json.dumps({'runs':rows}))
            fake=SimpleNamespace(digest=lambda:'fixture',source_files=lambda:[('main.py',results/'runs/first-1/baseline/main.py')])
            with patch('starter_support.load_starter',return_value=fake):
                result=export(results,repo,output,'review/test')
            self.assertEqual(git('rev-parse','HEAD'),original);self.assertEqual(git('status','--porcelain'),'')
            self.assertEqual(len(result['commits']),4)
            for c in cells:
                item=result['commits'][c['run_id']];path=item['path']+'/starter/main.py';commit=item['commit']
                baseline=(results/'runs'/c['run_id']/'baseline/main.py').read_text().strip()
                self.assertEqual(git('show',commit+'^:'+path),baseline)
                if item['source_available']:
                    self.assertEqual(git('show',commit+':'+path),c['run_id']);self.assertTrue(git('ls-tree',commit,'--',path).startswith('100755'))
                else:
                    self.assertEqual(git('show',commit+':'+path),baseline)
                    receipt=json.loads(git('show',commit+':'+item['path']+'/stage.json'));self.assertFalse(receipt['source_available']);self.assertIn('NOT',receipt['archive_note'])
if __name__=='__main__':unittest.main()
