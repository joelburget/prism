"""Two frozen checkpoints per independent native subscription rollout."""
import argparse
import fcntl
import json
from pathlib import Path
import tempfile
import uuid

from . import native_batch as native
from .control import file_hash, inputs_fingerprint, json_bytes, load_starter, tree_fingerprint
from .results import ResultStore
from followups.export_public import export as export_followup
from followups.evaluator.grade import grade as grade_followup
from followups.evaluator.freeze import snapshot as followup_snapshot

HARNESS = 'subscription-native-chain-v1'
TASKS = ('query-null', 'workflow-recovery')
PRISM_BRIEFING = Path(__file__).with_name('context') / 'prism-0.18.md'
BRIEFING_PROFILE = 'prism-brief-v1'
QUESTIONS = {
    'query-null': 'Explain how one atomic batch updates both sides of a join, preserves duplicates, and retracts the last match. Identify where unaffected views avoid recomputation.',
    'workflow-recovery': 'Trace an expired execute response racing with cancellation and a replacement lookup. Explain the roles of the action key, attempt number and lease ticket, citing the commit checks.',
}


def fingerprints():
    return {'original': inputs_fingerprint(), 'followups': followup_snapshot(),
            'prism_briefing_sha256': file_hash(PRISM_BRIEFING)}


def stage_two_prompt(task, language):
    return f'''Implement checkpoint two of the existing {language} {task} program.
Read {task}/PREVIOUS.md and {task}/PROBLEM.md, then extend starter/.
The source is the frozen preceding submission; inspect and preserve its existing
behavior while implementing the follow-up. Only source crosses checkpoints.
All earlier public cases are baseline regressions. Pass both baseline and extension
cases using python3 run.py run --task {task} --command './starter/run.sh'.
Keep production code in {language}. Preserve the launch/build entrypoints.
For Prism, run ./starter/build.sh after edits; language docs are in /opt/prism-docs/spec.md
and /opt/prism-docs/stdlib, with library source in /opt/prism-lib/std. Do not delegate production behavior to
another language, another SQL engine, fixture lookups, or the public test driver.
For TypeScript, check with tsc --noEmit --typeRoots /opt/typescript/node_modules/@types.
All file reads, edits and commands use execute in the writable task container:
/work and /work/starter are writable; apply_patch is installed and accepts a
patch on stdin. The native client filesystem is separate. Finish by describing
the changes and checks. Do not ask for clarification; use the published contract.
'''


def make_plan(root, model_keys=None, tasks=TASKS, languages=native.LANGUAGES,
              seed=1730, repetitions=1, performance=None, prism_briefing=False):
    if type(prism_briefing) is not bool: raise ValueError('prism_briefing must be boolean')
    if not tasks or set(tasks)-set(TASKS): raise ValueError('unknown follow-up task')
    store=ResultStore(root)
    if (store.root/'plan.json').exists(): raise ValueError('plan already exists')
    # Existing planner validates models, budgets, runtime images and routing.
    with tempfile.TemporaryDirectory(prefix='prism-chain-plan-') as temporary:
        plan=native.make_plan(Path(temporary)/'base',model_keys,tasks,languages,repetitions,seed)
        verification=json.loads((Path(temporary)/'base'/'NATIVE_VERIFICATION.json').read_text())
    briefing=PRISM_BRIEFING.read_text() if prism_briefing else None
    cells=[]
    for first in plan['runs']:
        first={**first,'checkpoint':1,'chain_id':'chain-'+uuid.uuid4().hex[:12]}
        second={**first,'checkpoint':2,'run_id':'run-'+uuid.uuid4().hex[:12],
                'parent_run_id':first['run_id'],'prompt':stage_two_prompt(first['task'],first['language'])}
        second.pop('starter_sha256')
        for cell in (first, second):
            enabled=briefing is not None and cell['language']=='prism'
            cell['language_context']=BRIEFING_PROFILE if enabled else 'baseline'
            if enabled:
                cell['prompt']=briefing+'\n\n---\n\n'+cell['prompt']
        cells.extend((first,second))
    plan.update(harness=HARNESS,inputs=fingerprints(),runs=cells,
                cohort='chain',performance_calibration_sha256=None,
                language_context={'profile':BRIEFING_PROFILE if prism_briefing else 'baseline',
                                  'prism_briefing_sha256':file_hash(PRISM_BRIEFING) if prism_briefing else None},
                notes=plan['notes']+['Fresh client session for each checkpoint; only assigned predecessor source is carried forward.',
                    'All finished valid predecessor sources continue, including incorrect ones. Invalid sources produce an explicit unattempted child.',
                    'Performance is graded separately after timed native runs; no feedback is supplied between checkpoints.'])
    if performance:
        calibration=json.loads(Path(performance).read_text())
        from followups.evaluator.performance import controls_fingerprint
        if calibration['task_image_id']!=plan['task_image_id']: raise ValueError('performance calibration image mismatch')
        if calibration['controls_sha256']!=controls_fingerprint(): raise ValueError('performance calibration controls changed')
        plan['performance_calibration_sha256']=file_hash(performance)
        (store.root/'performance-calibration.json').write_bytes(Path(performance).read_bytes())
        (store.root/'performance-calibration.json').chmod(0o400)
    for name,value in [('NATIVE_VERIFICATION.json',verification),('plan.json',plan)]:
        with (store.root/name).open('xb') as f: f.write(json_bytes(value))
        (store.root/name).chmod(0o400)
    return plan


def checked_plan(store):
    plan=json.loads((store.root/'plan.json').read_text())
    if plan['harness']!=HARNESS or plan['inputs']!=fingerprints(): raise ValueError('frozen chain inputs changed')
    for key in ('task_image_id','native_image_id'):
        if native._image_id(plan[key])!=plan[key]: raise ValueError('pinned image unavailable')
    verification=store.root/'NATIVE_VERIFICATION.json'
    if file_hash(verification)!=plan['verification_sha256']: raise ValueError('verification changed')
    native.checked_verification(verification,plan['native_image_id'],[c['model'] for c in plan['runs']],plan['task_image_id'])
    if plan['performance_calibration_sha256'] and file_hash(store.root/'performance-calibration.json')!=plan['performance_calibration_sha256']: raise ValueError('performance calibration changed')
    context=plan.get('language_context', {'profile':'baseline','prism_briefing_sha256':None})
    if context.get('profile') not in ('baseline',BRIEFING_PROFILE):
        raise ValueError('unknown language context profile')
    briefing_enabled=context['profile']==BRIEFING_PROFILE
    expected_digest=file_hash(PRISM_BRIEFING) if briefing_enabled else None
    if context.get('prism_briefing_sha256')!=expected_digest:
        raise ValueError('language briefing changed')
    prior={}
    for cell in plan['runs']:
        enabled=briefing_enabled and cell['language']=='prism'
        if cell.get('language_context','baseline')!=(BRIEFING_PROFILE if enabled else 'baseline'):
            raise ValueError('cell language context differs from plan')
        if enabled and not cell['prompt'].startswith(PRISM_BRIEFING.read_text()+'\n\n---\n\n'):
            raise ValueError('Prism prompt is missing its frozen briefing')
        if cell['run_id'] in prior: raise ValueError('duplicate run')
        if cell['checkpoint']==2:
            parent=prior.get(cell['parent_run_id'])
            if not parent or parent['checkpoint']!=1 or any(parent[k]!=cell[k] for k in ('task','language','model','effort','chain_id','repetition')): raise ValueError('invalid parent relationship')
        elif cell['checkpoint']!=1: raise ValueError('invalid checkpoint')
        prior[cell['run_id']]=cell
        directory=store.run_dir(cell['run_id'])
        if directory.exists():
            if not (directory/'result.json').exists(): raise ValueError('interrupted cell requires reconciliation; no automatic retry')
            if json.loads((directory/'result.json').read_text())['status']!='completed': raise ValueError('prior infrastructure failure stops this plan')
    return plan


def prepare_followup(store,cell,destination):
    parent=store.run_dir(cell['parent_run_id'])
    receipt=export_followup(cell['task'],destination,parent,'chain')
    store.write_artifact(cell['run_id'],'lineage.json',json_bytes(receipt))
    store.write_artifact(cell['run_id'],'previous-problem.md',(destination/cell['task']/'PREVIOUS.md').read_bytes())
    return destination


def second_grader(language):
    def grade(image,source,task,build):
        result=grade_followup(task,language,source,image)
        for report in result['scores'].values():
            report['passed']=all(case['status']=='pass' for case in report['cases'])
        return result['scores']
    return grade


def execute_plan(root,limit=None):
    if limit is not None and (type(limit) is not int or limit<1): raise ValueError('invalid limit')
    store=ResultStore(root)
    with (store.root/'execution.lock').open('a') as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise ValueError('chain controller already running')
        plan=checked_plan(store); finished=0; stopped=None
        for cell in plan['runs']:
            if (store.root/'STOP_AFTER_CURRENT').exists(): break
            if store.run_dir(cell['run_id']).exists(): continue
            kwargs={'harness':HARNESS,'starter_build':'build.sh' if cell['language']=='prism' else None}
            if cell['checkpoint']==1:
                if load_starter(cell['task'],cell['language']).digest()!=cell['starter_sha256']: raise ValueError('starter changed')
            else:
                parent=store.run_dir(cell['parent_run_id']); result=json.loads((parent/'result.json').read_text())
                if result.get('invalid_submission'):
                    store.create_run(cell['run_id'],{**cell,'harness':HARNESS,'comprehension_prompt':QUESTIONS[cell['task']]})
                    store.write_artifact(cell['run_id'],'source.patch','')
                    store.write_artifact(cell['run_id'],'problem.md',(Path(__file__).resolve().parents[1]/'followups'/cell['task']/'PROBLEM.md').read_bytes())
                    store.finish_run(cell['run_id'],{'status':'completed','success':None,'not_attempted':True,'stop_reason':'invalid_predecessor_source','estimated_cost_usd':None})
                    finished+=1
                    if limit and finished>=limit: break
                    continue
                if tree_fingerprint(parent/'source')!=result['source_sha256']: raise ValueError('parent source changed')
                kwargs.update(prepare=lambda output,c=cell:prepare_followup(store,c,output),grader=second_grader(cell['language']),comprehension=QUESTIONS[cell['task']])
            print(f"Checkpoint {cell['checkpoint']}: {cell['chain_id']}",flush=True)
            result=native.execute_cell(store,plan,cell,**kwargs)
            finished+=1
            print(f"Finished {cell['run_id']}: {result['status']}; success={result.get('success')}",flush=True)
            if result['status']=='infrastructure_error': stopped=cell['run_id']; break
            if limit and finished>=limit: break
        return {'runs_finished':finished,'stopped_on_infrastructure_run':stopped,'summary':summary(store)}


def summary(store):
    plan=json.loads((store.root/'plan.json').read_text()); rows=[]
    for cell in plan['runs']:
        path=store.run_dir(cell['run_id'])/'result.json'
        if path.exists(): rows.append({**cell,'result':json.loads(path.read_text())})
    children=[r for r in rows if r['checkpoint']==2]
    parents={r['run_id']:r for r in rows if r['checkpoint']==1}
    qualified=[r for r in children if parents[r['parent_run_id']]['result'].get('success') is True]
    return {'planned_stages':len(plan['runs']),'finished_stages':len(rows),
            'infrastructure_errors':sum(r['result']['status']=='infrastructure_error' for r in rows),
            'checkpoint_one_passed':sum(r['result'].get('success') is True for r in parents.values()),
            'checkpoint_two_passed':sum(r['result'].get('success') is True for r in children),
            'checkpoint_two_with_passing_parent':len(qualified),
            'both_checkpoints_passed':sum(r['result'].get('success') is True for r in qualified),
            'not_attempted':sum(bool(r['result'].get('not_attempted')) for r in children)}


def main():
    p=argparse.ArgumentParser(description=__doc__); sub=p.add_subparsers(dest='command',required=True)
    plan=sub.add_parser('plan'); plan.add_argument('--results',type=Path,required=True)
    plan.add_argument('--model',action='append'); plan.add_argument('--task',choices=TASKS,action='append')
    plan.add_argument('--language',choices=native.LANGUAGES,action='append'); plan.add_argument('--seed',type=int,default=1730)
    plan.add_argument('--repetitions',type=int,default=1); plan.add_argument('--performance-calibration',type=Path)
    plan.add_argument('--prism-briefing',action='store_true',help='Prepend the frozen Prism 0.18 briefing to both Prism checkpoints; other languages retain baseline prompts')
    run=sub.add_parser('run'); run.add_argument('--results',type=Path,required=True); run.add_argument('--limit',type=int)
    status=sub.add_parser('status'); status.add_argument('--results',type=Path,required=True)
    a=p.parse_args()
    if a.command=='plan':
        result=make_plan(a.results,a.model,a.task or TASKS,a.language or native.LANGUAGES,a.seed,a.repetitions,a.performance_calibration,a.prism_briefing)
        print(f"Frozen {len(result['runs'])} stages in {a.results}")
    elif a.command=='run':
        result=execute_plan(a.results,a.limit); print(json.dumps(result,indent=2))
        return 1 if result['stopped_on_infrastructure_run'] else 0
    else: print(json.dumps(summary(ResultStore(a.results)),indent=2))
    return 0

if __name__=='__main__': raise SystemExit(main())
