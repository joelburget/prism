#!/usr/bin/env python3
"""Run checkpoint-two acceptance with the existing language-neutral process driver."""
import importlib.util
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
source = ROOT / 'public_runner.py'
if not source.exists():
    source = ROOT.parent / 'run.py'
spec = importlib.util.spec_from_file_location('followup_process_runner', source)
runner = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = runner
spec.loader.exec_module(runner)
runner.ROOT = ROOT
runner.TASKS = ('query-null', 'workflow-recovery')
if __name__ == '__main__':
    raise SystemExit(runner.main())
