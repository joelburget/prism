#!/usr/bin/env python3
"""Export an allowlisted public task bundle without evaluator data or Git history."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tempfile

from run import ROOT, TASKS, load_cases


def export_public(task: str, destination: Path, root: Path = ROOT) -> Path:
    if task not in TASKS:
        raise ValueError(f"unknown task: {task}")
    destination = destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("destination must not already exist")
    # A source-repository worktree is not a clean agent input. Reject obvious
    # exports inside this repository; isolation from other mounts remains external.
    resolved = destination.resolve()
    repository = next((parent for parent in (root, *root.parents) if (parent / '.git').exists()), root)
    if resolved == repository.resolve() or repository.resolve() in resolved.parents:
        raise ValueError("export outside the evaluator repository; do not use another directory inside it")
    load_cases(root, [task])
    files = {"run.py": (root / "run.py").read_bytes(),
             f"{task}/PROBLEM.md": (root / task / "PROBLEM.md").read_bytes(),
             f"{task}/cases.json": (root / task / "cases.json").read_bytes()}
    files["README.md"] = f"""# Public change pilot: {task}

Read [{task}/PROBLEM.md]({task}/PROBLEM.md) for the baseline contract and requested
modification. This bundle contains public tests and their process runner. Supply
only the assigned language's starter in this run; starters are packaged separately.
Use a fresh agent session, with no access to another language's code or run artifacts.

The parent README referenced by the problem is this file. Agent filesystem/network
access must be restricted by the surrounding evaluation environment; a copied
directory alone does not restrict access to the evaluator's original repository.

## Run public tests

Requires Python 3.10+ on macOS or Linux. No third-party Python packages are needed.
From this directory:

```sh
python3 run.py validate --task {task}
python3 run.py list --task {task}
python3 run.py run --task {task} --phase baseline --command './path/to/implementation'
python3 run.py run --task {task} --command './path/to/implementation' --report report.json
```

Replace the command with the executable and arguments for your assigned language;
compile beforehand if needed. Commands are split into arguments without a shell.
Each case launches a fresh process, sends one JSON request on stdin, then closes
stdin. Return exactly one JSON response on stdout and exit zero, including domain
errors. Diagnostics go to stderr. The problem defines the input/result schema.
JSON object order and whitespace are ignored; array order and value types are
significant. Only integer JSON numbers are accepted. Duplicate keys, extra output,
extra fields, and nonzero process exits fail. Each stdout/stderr stream is limited
to 1 MiB. The default timeout is 10 seconds per case, configurable with --timeout.

Exit status is 0 for all selected cases passing, 1 for acceptance failures, and 2
for runner configuration/infrastructure errors. A completed change must pass both
baseline and extension cases. Public test iteration and agent-authored tests are
allowed. Additional evaluator tests use the same published semantics.

`MANIFEST.json` fingerprints the public files in this export. No starter, evaluator
corpus, evaluator reports, or Git metadata is included in this bundle.
""".encode("utf-8")
    manifest = {"schema_version": 1, "task": task,
                "files": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())}}
    files["MANIFEST.json"] = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".pilot-export-", dir=destination.parent))
    try:
        for name, data in files.items():
            path = temporary / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        # Do not merge into a preexisting folder that may contain private material.
        destination.mkdir()
        for path in temporary.iterdir():
            shutil.move(str(path), destination / path.name)
    finally:
        shutil.rmtree(temporary)
    return destination


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", required=True, choices=TASKS)
    parser.add_argument("--output", type=Path, required=True, help="new directory outside the evaluator repository")
    args = parser.parse_args(argv)
    try:
        destination = export_public(args.task, args.output)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Export failed: {exc}\n")
    print(f"Exported public {args.task} bundle to {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
