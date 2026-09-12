"""Continue a frozen chain after the diagnosed NUL-command classification bug.

No model is called here. Completed records are copied unchanged into a new results
root. The misclassified run is graded from its frozen source; its original result
and an evidence receipt are retained. The original experiment stays untouched.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import stat
import subprocess
import time

from . import chained_batch as chain
from . import native_batch as native
from .control import HERE, file_hash, grade_submission, json_bytes, tree_fingerprint
from .results import ResultStore

# No task, prompt, runtime, budget, scoring or source-capture changes are allowed
# when reconciling this particular defect. Other repairs need their own audit.
ALLOWED_INPUT_CHANGES = {'experiments/native_bridge.py', 'experiments/reconcile.py'}


def read(path):
    return json.loads(Path(path).read_text())


def check_tree(root):
    if root.is_symlink() or not root.is_dir():
        raise ValueError('record directory must be a regular directory')
    for path in root.rglob('*'):
        mode = path.lstat().st_mode
        if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            raise ValueError('record contains a symlink or special file')


def nul_evidence(directory, cell, result):
    """Require a finished client and only the exact pre-spawn NUL failure."""
    if (result.get('status') != 'infrastructure_error'
            or result.get('stop_reason') != 'completed'
            or result.get('exit_code') != 0
            or result.get('error_type') or result.get('invalid_submission')
            or result.get('scores') or result.get('tool_resource_limits')
            or result.get('observed_model_ids') != [cell['model']['model_id']]):
        raise ValueError('run is not eligible for NUL-error reconciliation')
    native_result = read(directory / 'native-result.json')
    if native_result.get('stop_reason') != 'completed' or native_result.get('exit_code') != 0:
        raise ValueError('native client did not complete normally')
    calls, failures, recovered_after = {}, [], False
    for line in (directory / 'events.jsonl').read_text().splitlines():
        event = json.loads(line)
        if event.get('event') == 'infrastructure_error':
            raise ValueError('additional infrastructure error recorded')
        if event.get('event') == 'tool_call':
            if event['call_id'] in calls:
                raise ValueError('duplicate recorded tool call')
            calls[event['call_id']] = event
        if event.get('event') != 'tool_result':
            continue
        call = calls.get(event['call_id'])
        if call is None:
            raise ValueError('unmatched recorded tool result')
        reply = event['result']
        if reply.get('isError'):
            if ('\0' not in call['command'] or reply.get('content') != [
                    {'type': 'text', 'text': 'Isolated execute failed: ValueError'}]):
                raise ValueError('tool error was not the diagnosed NUL failure')
            failures.append({'call_id': event['call_id'], 'reason': 'command_contains_nul'})
            recovered_after = False
        elif failures:
            recovered_after = True
    if not failures or not recovered_after:
        raise ValueError('no recovered NUL failure found')
    if tree_fingerprint(directory / 'source') != result.get('source_sha256'):
        raise ValueError('frozen source changed')
    return failures


def continue_nul_error(previous, output, run_id):
    previous, output = Path(previous).resolve(), Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise ValueError('continuation output must be new')
    if previous == output.resolve() or previous in output.resolve().parents:
        raise ValueError('continuation must be outside the previous results root')
    old_plan = read(previous / 'plan.json')
    if old_plan.get('harness') != chain.HARNESS or old_plan.get('continuation'):
        raise ValueError('expected an original frozen chain plan')
    current = chain.fingerprints()
    if old_plan['inputs']['followups'] != current['followups']:
        raise ValueError('follow-up evaluation inputs changed')
    old_inputs, new_inputs = old_plan['inputs']['original'], current['original']
    changed = {key for key in old_inputs.keys() | new_inputs.keys()
               if old_inputs.get(key) != new_inputs.get(key)}
    if not changed <= ALLOWED_INPUT_CHANGES:
        raise ValueError('unrelated experiment inputs changed')
    if file_hash(previous / 'NATIVE_VERIFICATION.json') != old_plan['verification_sha256']:
        raise ValueError('original verification receipt changed')
    for key in ('native_image_id', 'task_image_id'):
        if native._image_id(old_plan[key]) != old_plan[key]:
            raise ValueError('pinned image unavailable')
    verification = native.checked_verification(native.VERIFICATION, old_plan['native_image_id'],
        [c['model'] for c in old_plan['runs']], old_plan['task_image_id'])
    old_plan_hash = file_hash(previous / 'plan.json')
    snapshots, target_cell, unstarted = {}, None, False
    for cell in old_plan['runs']:
        directory = previous / 'runs' / cell['run_id']
        if not directory.exists():
            unstarted = True
            continue
        if unstarted or target_cell is not None:
            raise ValueError('run history is not a completed prefix followed by the interrupted run')
        check_tree(directory)
        metadata, result = read(directory / 'metadata.json'), read(directory / 'result.json')
        if any(metadata.get(k) != cell[k] for k in
               ('run_id', 'checkpoint', 'chain_id', 'task', 'language', 'model', 'effort', 'repetition')):
            raise ValueError('record metadata differs from frozen assignment')
        if metadata.get('plan_sha256') != old_plan_hash:
            raise ValueError('record has a different original plan')
        if result.get('source_sha256') and tree_fingerprint(directory / 'source') != result['source_sha256']:
            raise ValueError('recorded source changed')
        snapshots[cell['run_id']] = tree_fingerprint(directory)
        if cell['run_id'] == run_id:
            evidence = nul_evidence(directory, cell, result)
            target_cell, old_result = cell, result
        elif result.get('status') != 'completed':
            raise ValueError('another infrastructure error blocks continuation')
    if target_cell is None:
        raise ValueError('interrupted run not found')

    # Copy first, grade the isolated copy, and verify the original records again
    # before publishing a runnable plan. A partial output can never be resumed.
    store = ResultStore(output)
    for ident, digest in snapshots.items():
        source, destination = previous / 'runs' / ident, store.run_dir(ident)
        shutil.copytree(source, destination)
        if tree_fingerprint(destination) != digest:
            raise ValueError('copied record differs from original')
    target = store.run_dir(run_id)
    (target / 'result.json').rename(target / 'original-result.json')
    started = time.monotonic()
    if target_cell['checkpoint'] == 1:
        scores = grade_submission(old_plan['task_image_id'], target / 'source', target_cell['task'],
                                  'build.sh' if target_cell['language'] == 'prism' else None)
    else:
        scores = chain.second_grader(target_cell['language'])(old_plan['task_image_id'],
            target / 'source', target_cell['task'], 'build.sh' if target_cell['language'] == 'prism' else None)
    if tree_fingerprint(target / 'source') != old_result['source_sha256']:
        raise ValueError('source changed during reconciliation grading')
    receipt = {'reason': 'nul_command_misclassified_as_infrastructure',
               'reconciled_at': datetime.now(timezone.utc).isoformat(),
               'original_plan_sha256': old_plan_hash,
               'original_result_sha256': file_hash(target / 'original-result.json'),
               'source_sha256': old_result['source_sha256'], 'evidence': evidence,
               'model_reexecuted': False, 'grading_elapsed_seconds': time.monotonic() - started}
    store.write_artifact(run_id, 'reconciliation.json', json_bytes(receipt))
    store.finish_run(run_id, {**old_result, 'status': 'completed', 'scores': scores,
        'success': all(s['passed'] for s in scores.values()),
        'reconciliation': 'reconciliation.json', 'reconciled_at': receipt['reconciled_at']})
    for ident, digest in snapshots.items():
        if tree_fingerprint(previous / 'runs' / ident) != digest:
            raise ValueError('original record changed during continuation preparation')
    if file_hash(previous / 'plan.json') != old_plan_hash:
        raise ValueError('original plan changed during continuation preparation')

    plan = {**old_plan, 'created_at': receipt['reconciled_at'], 'inputs': current,
        'repository_revision': subprocess.run(['git', '-C', str(HERE), 'rev-parse', 'HEAD'],
            capture_output=True, text=True, check=True).stdout.strip(),
        'verification_sha256': hashlib.sha256(json_bytes(verification)).hexdigest(),
        'continuation': {'previous_results': str(previous), 'previous_plan_sha256': old_plan_hash,
            'inherited_records_sha256': snapshots, 'reconciled_run_id': run_id,
            'changed_inputs': sorted(changed), 'new_model_calls_during_preparation': 0},
        'notes': old_plan['notes'] + [
            'Continued after the NUL-command classification fix. Assignments, prompts and budgets are unchanged.',
            'Inherited records retain their original metadata and plan hashes. The misclassified run was graded from frozen source; its original result is preserved.']}
    if plan.get('performance_calibration_sha256'):
        calibration = previous / 'performance-calibration.json'
        if file_hash(calibration) != plan['performance_calibration_sha256']:
            raise ValueError('performance calibration changed')
        shutil.copy2(calibration, store.root / calibration.name)
    for name, data in [('NATIVE_VERIFICATION.json', verification), ('plan.json', plan)]:
        with (store.root / name).open('xb') as stream:
            stream.write(json_bytes(data))
        (store.root / name).chmod(0o400)
    chain.checked_plan(store)
    return {'results': str(store.root), 'inherited_stages': len(snapshots),
            'reconciled_run_id': run_id, 'remaining_stages': len(plan['runs']) - len(snapshots)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--previous', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--run', required=True)
    args = parser.parse_args()
    print(json.dumps(continue_nul_error(args.previous, args.output, args.run), indent=2))


if __name__ == '__main__':
    main()
