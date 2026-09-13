"""Read frozen evaluator records into a separate, shareable review report."""
import argparse
from collections import defaultdict
from datetime import datetime, timezone
import difflib
import hashlib
import json
from pathlib import Path
import statistics

CODE = {'.pr', '.py', '.ts'}

def read(path):
    return json.loads(Path(path).read_text())

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def code_metrics(before, after):
    if not after.is_dir():
        return dict(code_lines=None, baseline_lines=None, added_lines=None, removed_lines=None)
    def files(root):
        return {p.relative_to(root).as_posix():p.read_text().splitlines()
                for p in root.rglob('*') if p.is_file() and p.suffix in CODE}
    old, new = files(before), files(after)
    added = removed = 0
    for name in old.keys() | new.keys():
        for tag, a, b, c, d in difflib.SequenceMatcher(None, old.get(name, []), new.get(name, []), autojunk=False).get_opcodes():
            if tag != 'equal':
                removed += b-a
                added += d-c
    return dict(code_lines=sum(map(len,new.values())),baseline_lines=sum(map(len,old.values())),added_lines=added,removed_lines=removed)

def collect(root):
    plan = read(root/'plan.json')
    rows=[]
    for order,cell in enumerate(plan['runs'],1):
        d=root/'runs'/cell['run_id']; result=read(d/'result.json')
        if result['status']!='completed':
            raise ValueError('Only completed, reconciled runs can be exported')
        # Use the authoritative implementation of the frozen fingerprint.
        import sys
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
        from experiments.control import tree_fingerprint
        if (d/'source').is_dir() and tree_fingerprint(d/'source') != result['source_sha256']:
            raise ValueError('Frozen source fingerprint mismatch: '+cell['run_id'])
        flags=list(result.get('tool_resource_limits',[]))
        if result.get('stop_reason') != 'completed': flags.append(result['stop_reason'])
        if result.get('invalid_submission'): flags.append('invalid_submission')
        scores={}
        for name in ['public','heldout']:
            cases=result.get('scores',{}).get(name,{}).get('cases')
            scores[name]=None if cases is None else {'passed':sum(c['status']=='pass' for c in cases),'total':len(cases),
                'failed_ids':[c['id'] for c in cases if c['status']!='pass']}
        perf_path=root/'performance'/(cell['run_id']+'.json')
        perf=read(perf_path) if perf_path.exists() else None
        row={k:cell[k] for k in ['run_id','chain_id','checkpoint','task','language','repetition']}
        row.update(order=order,language_context=cell.get('language_context','baseline'),model=cell['model']['key'],model_id=cell['model']['model_id'],provider=cell['model']['provider'],
                   effort=cell.get('effort'),parent_run_id=cell.get('parent_run_id'),passed=result.get('success') is True,
                   seconds=result.get('elapsed_seconds'),tool_calls=result.get('tool_calls'),flags=sorted(set(flags)),
                   stop_reason=result.get('stop_reason'),source_available=(d/'source').is_dir(),source_sha256=result.get('source_sha256'),
                   result_sha256=sha(d/'result.json'),scores=scores,
                   performance=None if perf is None else {k:perf.get(k) for k in ['screening_passed','not_measured','profiles']},
                   api_equivalent_cost_usd=result.get('api_equivalent_cost_usd'),usage={k:v for k,v in (result.get('usage') or {}).items() if k in {'input_tokens','output_tokens','cached_input_tokens','cache_creation_input_tokens','cache_read_input_tokens'}},
                   **code_metrics(d/'baseline',d/'source'))
        rows.append(row)
    by_id={r['run_id']:r for r in rows}
    for r in rows:
        r['parent_passed']=by_id[r['parent_run_id']]['passed'] if r['parent_run_id'] else None
    return {'schema_version':1,'generated_at':datetime.now(timezone.utc).isoformat(),
            'plan_sha256':sha(root/'plan.json'),'cohort':'2026-09-12 chained calibration',
            'notes':[
                'One rollout per model/task/language cell. Checkpoint two inherits the same model’s frozen checkpoint-one source in a fresh session.',
                'Behavioral pass requires every public and held-out case. Invalid archives count as failures; their source and code metrics are unavailable.',
                'Time is native-session elapsed time, including tools, excluding setup and external grading. It is not human coding time.',
                'Resource flags are retained. Unflagged medians exclude all flagged stages; pass rates always include them.',
                'Code lines and churn include all .pr/.py/.ts files, including model-written tests, blank lines and comments. They are review-size proxies, not quality scores.',
                'Performance screens cover two restricted query profiles and only behaviorally passing follow-ups. A screen pass is not a general incremental-algorithm proof.',
                'Token usage is provider-native and not normalized across providers. API-equivalent cost is an estimate, not subscription billing.',
                'Model identity is visible in this report and its GitHub links. No human review times or comprehension scores have been recorded by this exporter.'
            ],'runs':rows}

def median(values):
    values=[v for v in values if v is not None]
    return statistics.median(values) if values else None

def group(rows,keys):
    groups=defaultdict(list)
    for r in rows:groups[tuple(r[k] for k in keys)].append(r)
    out=[]
    for values,items in sorted(groups.items()):
        measured=[r for r in items if r['performance'] and r['performance']['screening_passed'] is not None]
        out.append({**dict(zip(keys,values)),'n':len(items),'passed':sum(r['passed'] for r in items),
            'flagged':sum(bool(r['flags']) for r in items),'median_minutes':median([r['seconds']/60 for r in items if r['seconds'] is not None]),
            'unflagged_median_minutes':median([r['seconds']/60 for r in items if not r['flags'] and r['seconds'] is not None]),
            'median_tool_calls':median([r['tool_calls'] for r in items]),'median_code_lines':median([r['code_lines'] for r in items]),
            'performance_passed':sum(r['performance']['screening_passed'] is True for r in measured),'performance_measured':len(measured)})
    return out

def render(data,output):
    output.mkdir(parents=True,exist_ok=True)
    git_file=output/'git-reviews.json'
    git=read(git_file) if git_file.exists() else None
    index_file=output/'prism/manifest.json'
    indexes=read(index_file) if index_file.exists() else None
    for r in data['runs']:
        if git:
            r['github_url']=git['commits'][r['run_id']]['url']
            r['github_commit']=git['commits'][r['run_id']]['commit']
        if indexes and r['run_id'] in indexes['runs']:
            r['prism_index']=indexes['runs'][r['run_id']]
            if r['prism_index'].get('after',{}).get('available'):
                r['prism_viewer_url']='viewer/viewer.html?src=../prism/'+r['run_id']+'/after.json'
                if r['prism_index'].get('diff',{}).get('available'):
                    r['prism_viewer_url']+='&diff=../prism/'+r['run_id']+'/diff.json'
    (output/'runs.json').write_text(json.dumps(data,indent=2,allow_nan=False)+'\n')
    template=Path(__file__).with_name('report.html').read_text()
    (output/'index.html').write_text(template.replace('/*REPORT_DATA*/',json.dumps(data,allow_nan=False).replace('<','\\u003c')))
    lines=['# Chained calibration results','',f"{len(data['runs'])} stages · {sum(r['passed'] for r in data['runs'])} behavioral passes",'']
    for title,keys in [('By model and checkpoint',['model','checkpoint']),('By problem, language and checkpoint',['task','language','checkpoint'])]:
        lines += ['## '+title,'','| '+' | '.join(keys)+' | Pass | Median min | Unflagged min | Flagged | Perf |','| '+' | '.join(['---']*(len(keys)+6))+' |']
        for g in group(data['runs'],keys):
            f=lambda x:'—' if x is None else f'{x:.1f}'
            lines.append('| '+' | '.join(str(g[k]) for k in keys)+f" | {g['passed']}/{g['n']} | {f(g['median_minutes'])} | {f(g['unflagged_median_minutes'])} | {g['flagged']} | {g['performance_passed']}/{g['performance_measured']} |")
        lines.append('')
    lines+=['## Interpretation','']+['- '+n for n in data['notes']]
    (output/'SUMMARY.md').write_text('\n'.join(lines)+'\n')

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--results',required=True,type=Path);p.add_argument('--output',required=True,type=Path)
    a=p.parse_args();render(collect(a.results),a.output)
