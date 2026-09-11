#!/usr/bin/env python3
"""Build and check baseline starters; held-out checks require explicit selection."""

import argparse
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from run import TASKS, corpus_digest, load_cases, positive_timeout, run_case, write_report
from starter_support import LANGUAGES, load_starter
from export_public import export_public

EXTENSION_WITNESSES = {
    "query-null": "three-valued-truth-tables",
    "workflow-recovery": "crash-after-effect",
    "ledger-refunds": "partial-refund-reopens-balance",
}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", action="append", choices=TASKS)
    parser.add_argument("--language", action="append", choices=LANGUAGES)
    parser.add_argument("--corpus", choices=("public", "heldout", "both"), default="public")
    parser.add_argument("--build", action="store_true", help="run each starter's build script once before testing")
    parser.add_argument("--exported", action="store_true", help="build/run fresh single-language public exports (requires --build)")
    parser.add_argument("--verify-incomplete", action="store_true", help="also require a valid response that fails one positive extension case per task")
    parser.add_argument("--timeout", type=positive_timeout, default=10)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    if args.exported and not args.build:
        parser.error("--exported requires --build because exports contain no compiled artifacts")
    tasks = args.task or TASKS
    languages = args.language or LANGUAGES
    corpus_names = ("public", "heldout") if args.corpus == "both" else (args.corpus,)
    temporary = None
    try:
        if args.exported:
            temporary = tempfile.TemporaryDirectory(prefix="pilot-starter-exports-")
        corpora = {name: [case for case in load_cases(ROOT if name == "public" else ROOT / "heldout", tasks)
                          if case.phase == "baseline"] for name in corpus_names}
        witnesses = {case.task: case for case in load_cases(ROOT, tasks)
                     if case.id == EXTENSION_WITNESSES[case.task]}
        variants = []
        passed = True
        for task in tasks:
            for language in languages:
                starter = load_starter(task, language)
                run_directory = starter.directory
                if temporary:
                    bundle = export_public(task, Path(temporary.name) / f"{task}-{language}", language=language)
                    run_directory = bundle / "starter"
                if args.build and starter.build:
                    print(f"Building {task}/{language}", flush=True)
                    subprocess.run([str(run_directory / starter.build)], cwd=run_directory,
                                   check=True, timeout=600)
                variant = {"task": task, "language": language, "starter_sha256": starter.digest(),
                           "exported": args.exported, "corpora": {}}
                command = [str(run_directory / starter.entrypoint)]
                for name, all_cases in corpora.items():
                    cases = [case for case in all_cases if case.task == task]
                    results = [run_case(case, command, args.timeout, str(run_directory)) for case in cases]
                    successes = sum(result["status"] == "pass" for result in results)
                    passed = passed and successes == len(cases)
                    print(f"{task}/{language} {name} baseline: {successes}/{len(cases)}", flush=True)
                    for result in results:
                        if result["status"] != "pass":
                            print(f"  {result['id']}: {result.get('reason', result['status'])}", flush=True)
                    variant["corpora"][name] = {"passed": successes, "total": len(cases),
                                                "corpus_sha256": corpus_digest(cases), "cases": results}
                if args.verify_incomplete:
                    result = run_case(witnesses[task], command, args.timeout, str(run_directory))
                    incomplete = result["status"] == "fail" and "actual" in result
                    passed = passed and incomplete
                    variant["extension_not_implemented"] = {"verified": incomplete, "witness": result}
                    print(f"{task}/{language} extension remains unimplemented: {incomplete}", flush=True)
                variants.append(variant)
        if args.report:
            write_report(args.report, {"schema_version": 1, "passed": passed, "variants": variants})
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(2, f"Starter verification failed: {exc}\n")
    finally:
        if temporary:
            temporary.cleanup()
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
