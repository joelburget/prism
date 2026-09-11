#!/usr/bin/env python3
"""Check (default), or deliberately refresh (--write), the evaluator corpus manifest."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from run import TASKS, corpus_digest, first_difference, load_cases, write_report

MANIFEST = ROOT / "heldout" / "MANIFEST.json"


def snapshot():
    files = ["run.py"]
    for task in TASKS:
        files.extend([f"{task}/PROBLEM.md", f"{task}/cases.json", f"heldout/{task}/cases.json"])
    corpora = {}
    for name, directory in (("public", ROOT), ("heldout", ROOT / "heldout")):
        cases = load_cases(directory)
        counts = {task: dict(Counter(case.phase for case in cases if case.task == task)) for task in TASKS}
        corpora[name] = {"count": len(cases), "sha256": corpus_digest(cases), "tasks": counts}
    return {"schema_version": 1, "corpora": corpora,
            "files": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in files}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="replace the manifest after an intentional corpus/spec/runner revision")
    args = parser.parse_args(argv)
    try:
        actual = snapshot()
        if args.write:
            write_report(MANIFEST, actual)
            print("Wrote evaluator manifest. Use a new experiment revision after changing a frozen corpus.")
            return 0
        expected = json.loads(MANIFEST.read_text())
        difference = first_difference(expected, actual)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Manifest check failed: {exc}\n")
    if difference:
        print(f"Frozen evaluation inputs changed: {difference}", file=sys.stderr)
        return 1
    print("Evaluator manifest matches both corpora, published specs, and runner.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
