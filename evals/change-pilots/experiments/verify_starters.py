"""Build all nine isolated starters and check baseline acceptance and incompleteness."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile

from .native_task_setup import DEFAULT_IMAGE
from .sandbox import DockerEvaluation, image_identity
from .control import ROOT, export_public, load_starter
from run import TASKS, load_cases, response_contract, parse_json, first_difference
from starter_support import LANGUAGES
from starters.check import EXTENSION_WITNESSES


def verify(image):
    identity = image_identity(image)['id']
    corpora = {name: load_cases(path) for name, path in [('public', ROOT), ('heldout', ROOT/'heldout')]}
    report = {'verified_at': datetime.now(timezone.utc).isoformat(), 'task_image_id': identity,
              'variants': [], 'model_inference_requests': 0}
    with tempfile.TemporaryDirectory(prefix='prism-runtime-baselines-') as temporary:
        for task in TASKS:
            for language in LANGUAGES:
                starter = load_starter(task, language)
                bundle = export_public(task, Path(temporary)/f'{task}-{language}', language=language)
                row = {'task': task, 'language': language, 'starter_sha256': starter.digest(), 'public': 0, 'heldout': 0}
                with DockerEvaluation(identity, bundle/'starter', build=starter.build) as evaluator:
                    def difference(case):
                        result = evaluator.run_input((json.dumps(case.request)+'\n').encode())
                        if result['timed_out'] or result['output_limited'] or result['exit_code']:
                            raise ValueError(f'{task}/{language}/{case.id}: execution failed: {result}')
                        actual = parse_json(result['stdout'])
                        response_contract(actual)
                        return first_difference(case.expect, actual)
                    for corpus, cases in corpora.items():
                        for case in cases:
                            if case.task != task or case.phase != 'baseline':
                                continue
                            mismatch = difference(case)
                            if mismatch is not None:
                                raise ValueError(f'{task}/{language}/{case.id}: {mismatch}')
                            row[corpus] += 1
                    witness = next(c for c in corpora['public'] if c.task == task and c.id == EXTENSION_WITNESSES[task])
                    if difference(witness) is None:
                        raise ValueError(f'{task}/{language}: starter unexpectedly implements the extension witness')
                    row['extension_remains_unimplemented'] = True
                report['variants'].append(row)
                print(json.dumps(row), flush=True)
    report['passed'] = True
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', default=DEFAULT_IMAGE)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('output already exists; use a fresh verification receipt')
    report = verify(args.image)
    with args.output.open('x') as stream:
        json.dump(report, stream, indent=2)
        stream.write('\n')


if __name__ == '__main__':
    main()
