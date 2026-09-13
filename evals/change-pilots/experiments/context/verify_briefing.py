"""Check all briefing snippets and execute complete programs; no model inference."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

BRIEFING = Path(__file__).with_name('prism-0.22.md')


def snippets(text):
    pattern = r'```prism(?:,([^\n]*))?\n(.*?)```(?:\s*```output\n(.*?)```)?'
    for index, match in enumerate(re.finditer(pattern, text, re.S), 1):
        yield index, set((match[1] or '').split(',')), match[2], match[3]


def verify(compiler, briefing=BRIEFING):
    version = subprocess.check_output([compiler, '--version'], text=True, timeout=30).strip()
    if version != 'prism 0.22.0':
        raise ValueError(f'Briefing requires prism 0.22.0, got {version}')
    records = []
    with tempfile.TemporaryDirectory(prefix='prism-context-check-') as temporary:
        for index, flags, code, expected in snippets(briefing.read_text()):
            record = {'index': index, 'source_sha256': hashlib.sha256(code.encode()).hexdigest()}
            records.append(record)
            if 'ignore' in flags:
                record.update(status='excluded', reason='Explicit upstream project-dependent/typed-hole fragment')
                continue
            source = Path(temporary) / f'example-{index}.pr'
            source.write_text(code)
            checked = subprocess.run([compiler, 'check', str(source)], capture_output=True, text=True, timeout=90)
            if 'compile_fail' in flags:
                if checked.returncode == 0:
                    raise ValueError(f'Example {index} unexpectedly typechecked')
                record['status'] = 'expected_compile_failure'
                continue
            if checked.returncode != 0:
                raise ValueError(f'Example {index} check failed: {checked.stderr}{checked.stdout}')
            if 'no_run' in flags or not re.search(r'^fn main\(', code, re.M):
                record['status'] = 'typechecked'
                continue
            executable = Path(temporary) / f'example-{index}'
            compiled = subprocess.run([compiler, str(source), '-o', str(executable)],
                                      capture_output=True, text=True, timeout=90)
            if compiled.returncode != 0:
                raise ValueError(f'Example {index} native build failed: {compiled.stderr}{compiled.stdout}')
            ran = subprocess.run([str(executable)], capture_output=True, text=True, timeout=15)
            if ran.returncode != 0 or (expected is not None and ran.stdout != expected):
                raise ValueError(f'Example {index} output mismatch: expected {expected!r}, got {ran.stdout!r}; {ran.stderr}')
            record.update(status='executed', stdout=ran.stdout, output_checked=expected is not None)
    return {'verified_at': datetime.now(timezone.utc).isoformat(), 'profile': 'prism-tutorial-v2',
            'briefing_sha256': hashlib.sha256(briefing.read_bytes()).hexdigest(),
            'compiler_version': version, 'compiler_binary_sha256': hashlib.sha256(Path(shutil.which(compiler)).read_bytes()).hexdigest(),
            'examples': records, 'model_inference_requests': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compiler', default='prism')
    parser.add_argument('--briefing', type=Path, default=BRIEFING)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    rendered = json.dumps(verify(args.compiler, args.briefing), indent=2) + '\n'
    if args.output:
        args.output.write_text(rendered)
    else:
        print(rendered, end='')


if __name__ == '__main__':
    main()
