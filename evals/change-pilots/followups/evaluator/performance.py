"""Isolated calibration and post-run screening of incremental-query scaling.

Controls implement only the published scaling profile, not the full query task.
A cutoff is reported only when separated reference ranges leave a safety margin.
Passing a profile is not proof of incremental architecture or general performance.
"""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import math
import os
import platform
from pathlib import Path
import shutil
import statistics
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT.parent))
from experiments.sandbox import DockerEvaluation, image_identity
try:
    from .scaling import workload
    from ..run import runner
except ImportError:
    from scaling import workload
    from run import runner

CONTROLS=Path(__file__).resolve().parent/'controls'
PROFILES=('point-updates','unrelated-table')


def controls_fingerprint():
    paths=[p for p in CONTROLS.rglob('*') if p.is_file() and '__pycache__' not in p.parts]
    paths.extend([Path(__file__),Path(__file__).with_name('scaling.py')])
    return {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}


def prepare_control(destination,language,mode):
    shutil.copytree(CONTROLS/language,destination)
    if language=='prism':
        main=destination/'main.pr'
        main.write_text(main.read_text().replace('let incremental : Bool = true','let incremental : Bool = '+str(mode=='incremental').lower()))
        (destination/'build.sh').write_text('#!/bin/sh\nset -eu\ncd "$(dirname "$0")"\nmkdir -p .build\nexec prism build . -o .build/control\n')
        (destination/'build.sh').chmod(0o755)
        command='exec ./.build/control'
    else:
        command=('exec python3 main.py ' if language=='python' else 'exec node main.ts ')+mode
    (destination/'run.sh').write_text('#!/bin/sh\nset -eu\ncd "$(dirname "$0")"\n'+command+'\n')
    (destination/'run.sh').chmod(0o755)
    return destination


def sample(evaluator,size,updates,profile):
    request,expected=workload(size,updates,profile)
    execution=evaluator.run_input((json.dumps(request)+'\n').encode(),timeout_seconds=120)
    if execution['timed_out'] or execution['output_limited'] or execution['exit_code'] or execution.get('stdout_invalid_utf8'):
        return {'correct':False,'elapsed_seconds':execution['elapsed_seconds'],'execution':execution}
    try:
        actual=runner.parse_json(execution['stdout']); runner.response_contract(actual)
        difference=runner.first_difference(expected,actual)
        return {'correct':difference is None,'elapsed_seconds':execution['elapsed_seconds'],'difference':difference}
    except ValueError as e:
        return {'correct':False,'elapsed_seconds':execution['elapsed_seconds'],'difference':str(e)}


def measure(evaluator,profile,size,updates,repetitions):
    samples=[]
    for _ in range(repetitions+1):
        samples.append(sample(evaluator,size,updates,profile))
        if not samples[-1]['correct']: break
    times=[s['elapsed_seconds'] for s in samples[1:]]
    return {'profile':profile,'size':size,'updates':updates,'samples':samples,
            'correct':len(samples)==repetitions+1 and all(s['correct'] for s in samples),
            'median_seconds':statistics.median(times) if times else None}


def threshold(fast,slow):
    if not fast['correct'] or not slow['correct']: return None
    fast_max=max(s['elapsed_seconds'] for s in fast['samples'][1:])
    slow_min=min(s['elapsed_seconds'] for s in slow['samples'][1:])
    # Both reference ranges must be at least 1.5x from the proposed boundary.
    if slow_min<=2.25*fast_max: return None
    return math.sqrt(fast_max*slow_min)


def calibrate(image,output,repetitions=3,size=9999,updates=999):
    if Path(output).exists(): raise ValueError('calibration already exists')
    identity=image_identity(image)['id']; report={'schema_version':1,'task_image_id':identity,
        'created_at':datetime.now(timezone.utc).isoformat(),'host':{'platform':platform.platform(),'machine':platform.machine(),'cpu_count':os.cpu_count()},
        'controls_sha256':controls_fingerprint(),'size':size,'updates':updates,'repetitions':repetitions,
        'scope':'Restricted-profile controls; screening only, not a full SQL qualification.',
        'policy':'geometric mean between reference ranges; null unless a 1.5x margin exists on both sides',
        'languages':{}}
    with tempfile.TemporaryDirectory(prefix='prism-performance-controls-') as temporary:
        for language in ('python','typescript','prism'):
            measurements={}
            with ExitStack() as stack:
                evaluators={mode:stack.enter_context(DockerEvaluation(identity,prepare_control(Path(temporary)/(language+'-'+mode),language,mode),build='build.sh' if language=='prism' else None)) for mode in ('incremental','recompute')}
                for profile in PROFILES:
                    measurements[profile]={}
                    for mode,evaluator in evaluators.items():
                        print(f'Calibrating {language}/{profile}/{mode}',flush=True)
                        measurements[profile][mode]=measure(evaluator,profile,size,updates,repetitions)
                    pair=measurements[profile]
                    pair['screening_cutoff_seconds']=threshold(pair['incremental'],pair['recompute'])
                    print(f"Cutoff {language}/{profile}: {pair['screening_cutoff_seconds']}",flush=True)
            report['languages'][language]=measurements
    Path(output).parent.mkdir(parents=True,exist_ok=True)
    with Path(output).open('x') as f: json.dump(report,f,indent=2); f.write('\n')
    return report


def assess(root):
    root=Path(root); plan=json.loads((root/'plan.json').read_text())
    calibration=json.loads((root/'performance-calibration.json').read_text())
    if calibration['task_image_id']!=plan['task_image_id'] or calibration['controls_sha256']!=controls_fingerprint(): raise ValueError('calibration inputs changed')
    if hashlib.sha256((root/'performance-calibration.json').read_bytes()).hexdigest()!=plan['performance_calibration_sha256']: raise ValueError('calibration receipt changed')
    if any(not (root/'runs'/c['run_id']/'result.json').exists() for c in plan['runs']): raise ValueError('finish timed native stages before performance measurements')
    directory=root/'performance'; directory.mkdir(exist_ok=True)
    for cell in plan['runs']:
        if cell['checkpoint']!=2 or cell['task']!='query-null': continue
        target=directory/(cell['run_id']+'.json')
        if target.exists(): continue
        run=root/'runs'/cell['run_id']; result=json.loads((run/'result.json').read_text())
        record={'run_id':cell['run_id'],'language':cell['language'],'source_sha256':result.get('source_sha256'),'profiles':{},'screening_passed':None}
        if result.get('success') is not True:
            record['not_measured']='behavioral acceptance did not pass'
        else:
            from experiments.control import tree_fingerprint
            if tree_fingerprint(run/'source')!=result['source_sha256']: raise ValueError('frozen source changed')
            with DockerEvaluation(plan['task_image_id'],run/'source',build='build.sh' if cell['language']=='prism' else None) as evaluator:
                for profile in PROFILES:
                    print(f"Measuring {cell['run_id']}/{profile}",flush=True)
                    measured=measure(evaluator,profile,calibration['size'],calibration['updates'],calibration['repetitions'])
                    cutoff=calibration['languages'][cell['language']][profile]['screening_cutoff_seconds']
                    measured['cutoff_seconds']=cutoff
                    measured['screening_passed']=None if cutoff is None else measured['correct'] and measured['median_seconds']<=cutoff
                    record['profiles'][profile]=measured
            if tree_fingerprint(run/'source')!=result['source_sha256']: raise ValueError('source changed while measuring')
            flags=[p['screening_passed'] for p in record['profiles'].values()]
            record['screening_passed']=False if False in flags else None if None in flags else True
        with target.open('x') as f: json.dump(record,f,indent=2); f.write('\n')
    return {'reports':len(list(directory.glob('*.json')))}


def main():
    p=argparse.ArgumentParser(description=__doc__); sub=p.add_subparsers(dest='command',required=True)
    c=sub.add_parser('calibrate'); c.add_argument('--image',required=True); c.add_argument('--output',type=Path,required=True)
    c.add_argument('--repetitions',type=int,default=3); c.add_argument('--size',type=int,default=9999); c.add_argument('--updates',type=int,default=999)
    a=sub.add_parser('assess'); a.add_argument('--results',type=Path,required=True)
    args=p.parse_args()
    if args.command=='calibrate':
        if not 1<=args.repetitions<=20 or not 1<=args.size<=9999 or not 1<=args.updates<=999: p.error('invalid calibration dimensions')
        calibrate(args.image,args.output,args.repetitions,args.size,args.updates)
    else: print(json.dumps(assess(args.results)))

if __name__=='__main__': main()
