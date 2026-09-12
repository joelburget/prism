#!/usr/bin/env python3
"""Grade a frozen second-checkpoint source in network-disabled fresh containers."""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
try:
    from ..run import runner
    from ..export_public import source_files, digest
except ImportError:
    from run import runner
    from export_public import source_files, digest
sys.path.insert(0,str(ROOT.parent))
from experiments.sandbox import DockerEvaluation, SubmissionBuildError, image_identity


def grade_case(case,execution):
    result={'id':case.id,'task':case.task,'phase':case.phase,'status':'fail','elapsed_seconds':execution.get('elapsed_seconds')}
    if execution.get('timed_out'): result['reason']='case timeout'
    elif execution.get('output_limited'): result['reason']='output limit exceeded'
    elif execution.get('stdout_invalid_utf8'): result['reason']='stdout is not UTF-8'
    elif execution.get('exit_code')!=0: result['reason']='nonzero process exit'
    else:
        try:
            actual=runner.parse_json(execution['stdout']); runner.response_contract(actual)
            difference=runner.first_difference(case.expect,actual)
            result.update(actual=actual,status='fail' if difference else 'pass')
            if difference: result['reason']=difference
        except (ValueError,KeyError) as e: result['reason']=str(e)
    return result


def grade(task,language,source,image,phase='all',factory=DockerEvaluation):
    before=source_files(source)[2]
    selected={name:[c for c in runner.load_cases(base,[task]) if phase=='all' or c.phase==phase] for name,base in [('public',ROOT),('heldout',ROOT/'evaluator'/'heldout')]}
    scores={}; build_error=None
    try:
        with factory(image,source,build='build.sh' if language=='prism' else None) as evaluator:
            for name,cases in selected.items():
                scores[name]=[grade_case(c,evaluator.run_input((json.dumps(c.request)+'\n').encode(),timeout_seconds=10)) for c in cases]
    except SubmissionBuildError as e:
        build_error=e.build_result
        scores={name:[{'id':c.id,'task':c.task,'phase':c.phase,'status':'not_run','reason':'submission build failed'} for c in cases] for name,cases in selected.items()}
    if source_files(source)[2]!=before: raise ValueError('source changed during grading; discard this grading attempt')
    return {'schema_version':1,'checkpoint':2,'task':task,'language':language,'source_sha256':before,'image_id':image,'status':'completed','phase_selection':phase,'full_corpus_selected':phase=='all','algorithmic_qualification':None,'build_error':build_error,'success':all(r['status']=='pass' for rows in scores.values() for r in rows),'scores':{name:{'corpus_sha256':runner.corpus_digest(selected[name]),'cases':rows,'phases':{p:{'passed':sum(r['status']=='pass' for r in rows if r['phase']==p),'total':sum(r['phase']==p for r in rows)} for p in ('baseline','extension')}} for name,rows in scores.items()}}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--task',required=True,choices=runner.TASKS); p.add_argument('--language',required=True,choices=['prism','python','typescript'])
    p.add_argument('--source',type=Path,required=True); p.add_argument('--image',required=True)
    p.add_argument('--phase',choices=['all','baseline','extension'],default='all')
    p.add_argument('--report',type=Path,required=True); a=p.parse_args()
    repository=next((x for x in (ROOT,*ROOT.parents) if (x/'.git').exists()),ROOT)
    if a.report.resolve()==repository or repository in a.report.resolve().parents: p.error('keep evaluation reports outside Git')
    if a.report.resolve()==a.source.resolve() or a.source.resolve() in a.report.resolve().parents: p.error('report must stay outside the frozen source')
    if a.report.exists() or a.report.is_symlink(): p.error('report already exists')
    # Reject altered evaluator inputs before executing any submission.
    from freeze import snapshot
    frozen=json.loads((ROOT/'evaluator'/'MANIFEST.json').read_text())
    if snapshot()!=frozen: p.error('checkpoint-two manifest changed; explicitly freeze a new revision first')
    try:
        identity=image_identity(a.image)
        result=grade(a.task,a.language,a.source,identity['id'],a.phase)
        result['evaluator_manifest_sha256']=digest((ROOT/'evaluator'/'MANIFEST.json').read_bytes())
    except Exception as e:
        result={'schema_version':1,'checkpoint':2,'status':'infrastructure_error','success':None,'reason':str(e)}
    a.report.parent.mkdir(parents=True,exist_ok=True)
    with a.report.open('x') as f: json.dump(result,f,indent=2); f.write('\n')
    print(json.dumps({k:result[k] for k in ('status','success')}))
    return 2 if result['status']=='infrastructure_error' else (0 if result['success'] else 1)

if __name__=='__main__': raise SystemExit(main())
