#!/usr/bin/env python3
"""Plan, execute, grade and review independent programming-agent rollouts."""

import argparse
from datetime import datetime, timezone
import difflib
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import time
import uuid

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
from run import TASKS, corpus_digest, first_difference, load_cases, parse_json, response_contract
from starter_support import LANGUAGES, load_starter
from export_public import export_public
from experiments.costs import estimate, format_estimate, select_models
from experiments.results import ResultStore

DEFAULT_IMAGE = "prism-change-pilots:0.18.0-py3.14.7-node25.2.1"
COMPREHENSION = {
    "query-null": "Explain how this change handles an unmatched left-join row and a NULL predicate, including whether optimization preserves the result. Cite the relevant functions.",
    "workflow-recovery": "Explain what happens if execution crashes after an external effect but before recording completion. How does this implementation avoid duplicate effects on restart? Cite the relevant functions.",
    "ledger-refunds": "Explain how a refund or reversal changes invoice balances and historical observations, and what prevents a rejected operation from partly changing state. Cite the relevant functions.",
}


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def inputs_fingerprint():
    files = [ROOT / "run.py", ROOT / "export_public.py", ROOT / "starter_support.py"]
    files += list(HERE.glob("*.py")) + [HERE / "models.json", HERE / "Dockerfile"]
    for task in TASKS:
        files += [ROOT / task / "PROBLEM.md", ROOT / task / "cases.json", ROOT / "heldout" / task / "cases.json"]
    return {str(p.relative_to(ROOT)): file_hash(p) for p in sorted(files)}


def prompt_for(task, language):
    return f"""Implement the requested extension to the existing {language} program.
Your only workspace is /work. Read README.md, {task}/PROBLEM.md, and starter/README.md.
Preserve the baseline behavior and public interfaces, and implement the full published extension.
Modify the existing starter rather than replacing it with a fixture lookup or another language.
The evaluator will build and run starter/run.sh. Keep its launch interface and the existing build.sh
interface when present. Only files under starter/ are submitted. Add your own tests there if useful.
Use the execute tool to read/edit files, compile, and run tests. Public test iteration is allowed;
run python3 run.py run --task {task} --command './starter/run.sh' after building if necessary.
The evaluator has additional cases of the published contract; you will not receive that feedback.
Offline Prism reference material is in /opt/prism-docs and the standard library in /opt/prism-lib.
Python and Node documentation/help and installed TypeScript checking tools are available locally.
For TypeScript checking, run tsc --noEmit --typeRoots /opt/typescript/node_modules/@types in starter/.
Submitted files must be regular UTF-8 source/config/test files with extensions .pr, .py, .ts,
.json, .md, .sh, .toml, .txt, .lock, or .gitignore. Build/dependency/cache directories are excluded.
Do not change the contract or public tests. Do not fetch solutions or access other implementations.
Stop when your implementation is ready and briefly summarize the change and checks performed.
"""


def make_plan(root, model_keys=None, tasks=TASKS, languages=LANGUAGES, repetitions=3,
              seed=1729, image=DEFAULT_IMAGE, max_turns=100, wall_seconds=1800,
              per_run_usd=20.0):
    if repetitions < 1 or max_turns < 1 or not math.isfinite(wall_seconds) or wall_seconds <= 0:
        raise ValueError("repetitions, turns and wall time must be positive")
    if not math.isfinite(per_run_usd) or per_run_usd <= 0:
        raise ValueError("per-run budget must be positive")
    if not tasks or len(set(tasks)) != len(tasks) or set(tasks) - set(TASKS):
        raise ValueError("invalid/duplicate task selection")
    if not languages or len(set(languages)) != len(languages) or set(languages) - set(LANGUAGES):
        raise ValueError("invalid/duplicate language selection")
    store = ResultStore(root)
    path = store.root / "plan.json"
    if path.exists():
        raise ValueError("plan already exists; use a fresh results directory")
    models = select_models(model_keys)
    cells = [{"run_id": "run-" + uuid.uuid4().hex[:12], "task": task, "language": language,
              "model": model, "repetition": rep,
              "starter_sha256": load_starter(task, language).digest(),
              "prompt": prompt_for(task, language)}
             for task in tasks for language in languages for model in models
             for rep in range(1, repetitions + 1)]
    random.Random(seed).shuffle(cells)
    plan = {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
            "repository_revision": subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"],
                capture_output=True, text=True, check=True).stdout.strip(),
            "harness": "shared-api-v1", "image": image, "order_seed": seed,
            "inputs": inputs_fingerprint(), "runs": cells,
            "budgets": {"max_turns": max_turns, "wall_timeout_seconds": wall_seconds,
                        "max_output_tokens": 8192, "max_cost_usd": per_run_usd,
                        "max_input_tokens": 260000},
            "cost_scenarios": estimate(models, len(tasks), len(languages), repetitions),
            "notes": ["No model calls occur while planning.",
                      "Repetitions are independent conversations, not deterministic model seeds.",
                      "No automatic model fallback or retries of failed/ambiguous billable calls.",
                      "A whole-run spending reservation is required before each rollout."]}
    with path.open("xb") as f:
        f.write(json_bytes(plan))
    path.chmod(0o400)
    return plan


def source_patch(before, after):
    names = sorted({str(p.relative_to(directory)) for directory in (before, after)
                    for p in directory.rglob("*") if p.is_file()})
    output = []
    for name in names:
        a, b = before / name, after / name
        old, new = (a.read_bytes() if a.exists() else b""), (b.read_bytes() if b.exists() else b"")
        old_mode = bool(a.stat().st_mode & 0o111) if a.exists() else None
        new_mode = bool(b.stat().st_mode & 0o111) if b.exists() else None
        if old_mode != new_mode:
            output.append(f"Executable flag {name}: {old_mode} -> {new_mode}\n")
        if old != new or a.exists() != b.exists():
            output += difflib.unified_diff(old.decode("utf-8", errors="replace").splitlines(True),
                                          new.decode("utf-8", errors="replace").splitlines(True),
                                          fromfile=f"before/{name}", tofile=f"after/{name}")
    return "".join(output)


def tree_fingerprint(directory):
    values = {str(p.relative_to(directory)): {"sha256": file_hash(p),
              "executable": bool(p.stat().st_mode & 0o111)}
              for p in sorted(directory.rglob("*")) if p.is_file()}
    return hashlib.sha256(json_bytes(values)).hexdigest()


def grade_case(case, execution):
    result = {"task": case.task, "id": case.id, "phase": case.phase,
              "elapsed_seconds": execution.get("elapsed_seconds"), "status": "fail"}
    if execution.get("timed_out"):
        result["reason"] = "case timeout"
    elif execution.get("stdout_invalid_utf8"):
        result["reason"] = "stdout is not UTF-8"
    elif execution.get("output_limited"):
        result["reason"] = "output limit exceeded"
    elif execution.get("exit_code") != 0:
        result["reason"] = f"process exited {execution.get('exit_code')}"
    else:
        try:
            actual = parse_json(execution["stdout"])
            response_contract(actual)
            difference = first_difference(case.expect, actual)
            result.update(status="fail" if difference else "pass", actual=actual)
            if difference:
                result["reason"] = difference
        except (ValueError, KeyError) as exc:
            result["reason"] = f"invalid response: {exc}"
    return result


def grade_submission(image, source, task, build, evaluation_factory=None):
    from experiments.sandbox import SubmissionBuildError
    if evaluation_factory is None:
        from experiments.sandbox import DockerEvaluation
        evaluation_factory = DockerEvaluation
    reports = {}
    try:
        with evaluation_factory(image, source, build=build) as evaluator:
            for name, corpus in (("public", ROOT), ("heldout", ROOT / "heldout")):
                cases = load_cases(corpus, [task])
                results = [grade_case(case, evaluator.run_input(
                    (json.dumps(case.request) + "\n").encode(), timeout_seconds=10)) for case in cases]
                phases = {phase: {"passed": sum(r["status"] == "pass" for r in results if r["phase"] == phase),
                                  "total": sum(r["phase"] == phase for r in results)}
                          for phase in ("baseline", "extension")}
                reports[name] = {"corpus_sha256": corpus_digest(cases), "phases": phases, "cases": results,
                                 "passed": all(r["status"] == "pass" for r in results)}
    except SubmissionBuildError as exc:
        for name, corpus in (("public", ROOT), ("heldout", ROOT / "heldout")):
            cases = load_cases(corpus, [task])
            reports[name] = {"corpus_sha256": corpus_digest(cases), "passed": False,
                "build_failed": True, "build_result": exc.build_result,
                "phases": {phase: {"passed": 0, "total": sum(c.phase == phase for c in cases)}
                           for phase in ("baseline", "extension")},
                "cases": [{"task": c.task, "id": c.id, "phase": c.phase,
                           "status": "not_run", "reason": "submission build failed"} for c in cases]}
    return reports


def _image_id(image):
    return subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", image],
                          check=True, capture_output=True, text=True, timeout=30).stdout.strip()


def execute_plan(root, total_budget_usd, limit=None):
    if not math.isfinite(total_budget_usd) or total_budget_usd <= 0:
        raise ValueError("an explicit positive total API spending cap is required")
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    store = ResultStore(root)
    with (store.root / "execution.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("this experiment already has a running controller")
        return _execute_locked(store, total_budget_usd, limit)


def _execute_locked(store, total_budget_usd, limit):
    from experiments.agent import run_agent
    from experiments.sandbox import DockerSandbox, InvalidSubmissionError
    plan = json.loads((store.root / "plan.json").read_text())
    if plan["inputs"] != inputs_fingerprint():
        raise ValueError("experiment inputs changed since planning; create a new experiment")
    image_id = _image_id(plan["image"])
    environment_file = store.root / "environment.json"
    if environment_file.exists():
        if json.loads(environment_file.read_text())["image_id"] != image_id:
            raise ValueError("toolchain image changed; create a new experiment")
    else:
        environment_file.write_bytes(json_bytes({"image_id": image_id, "image": plan["image"]}))
    spent = 0.0
    for cell in plan["runs"]:
        directory = store.run_dir(cell["run_id"])
        if directory.exists():
            path = directory / "result.json"
            if not path.exists():
                raise ValueError("interrupted rollout has unknown billing; reconcile it before continuing")
            result = json.loads(path.read_text())
            if result.get("billing_uncertain") or result.get("billing_unknown"):
                raise ValueError("a prior rollout has uncertain billing; reconcile before continuing")
            spent += result.get("estimated_cost_usd", 0)
    completed = 0
    for cell in plan["runs"]:
        run_id = cell["run_id"]
        if store.run_dir(run_id).exists():
            continue
        reservation = plan["budgets"]["max_cost_usd"]
        if spent + reservation > total_budget_usd:
            print(f"Stopped before next rollout: ${spent:.2f} recorded; ${reservation:.2f} reservation exceeds remaining cap.")
            break
        model = cell["model"]
        key_name = "OPENAI_API_KEY" if model["provider"] == "openai" else "ANTHROPIC_API_KEY"
        if not os.environ.get(key_name):
            raise ValueError(f"{key_name} must be set in the controller environment")
        starter = load_starter(cell["task"], cell["language"])
        if starter.digest() != cell["starter_sha256"]:
            raise ValueError("starter changed since planning")
        metadata = {**cell, "harness": plan["harness"], "image_id": image_id,
                    "budgets": plan["budgets"], "plan_sha256": file_hash(store.root / "plan.json"),
                    "comprehension_prompt": COMPREHENSION[cell["task"]]}
        store.create_run(run_id, metadata)
        print(f"Starting {run_id}: {cell['task']}/{cell['language']}/{model['key']}", flush=True)
        agent_result = None
        started = time.monotonic()
        try:
            with tempfile.TemporaryDirectory(prefix="prism-rollout-") as temporary:
                bundle = export_public(cell["task"], Path(temporary) / "bundle", language=cell["language"])
                store.write_artifact(run_id, "input-manifest.json", (bundle / "MANIFEST.json").read_bytes())
                store.write_artifact(run_id, "problem.md", (bundle / cell["task"] / "PROBLEM.md").read_bytes())
                for original in (bundle / "starter").rglob("*"):
                    if original.is_file():
                        store.write_artifact(run_id, "baseline/" + str(original.relative_to(bundle / "starter")), original.read_bytes())
                with DockerSandbox(image_id, bundle) as sandbox:
                    agent_result = run_agent({**model, **plan["budgets"]}, cell["prompt"],
                                             sandbox.execute, lambda event: store.append_event(run_id, event))
                    sandbox.freeze()
                    source = sandbox.snapshot(store.run_dir(run_id) / "source")
                store.write_artifact(run_id, "source.patch", source_patch(bundle / "starter", source))
                final_hash = tree_fingerprint(source)
                scores = grade_submission(image_id, source, cell["task"], starter.build)
                success = all(report["passed"] for report in scores.values())
                infrastructure_failure = agent_result.get("stop_reason") in (
                    "provider_error", "provider_protocol_error", "tool_error")
                result = {**agent_result, "status": "infrastructure_error" if infrastructure_failure else "completed", "success": success,
                          "scores": scores, "source_sha256": final_hash, "patch": "source.patch",
                          "harness": plan["harness"], "total_elapsed_seconds": time.monotonic() - started}
        except InvalidSubmissionError:
            result = {**(agent_result or {}), "status": "completed", "success": False,
                      "invalid_submission": True, "billing_uncertain": agent_result is None,
                      "total_elapsed_seconds": time.monotonic() - started}
        except Exception as exc:
            # Avoid logging provider response bodies or arbitrary secret-bearing exception strings.
            result = {**(agent_result or {}), "status": "infrastructure_error", "success": False,
                      "error_type": type(exc).__name__, "billing_uncertain": agent_result is None,
                      "patch": "source.patch", "total_elapsed_seconds": time.monotonic() - started}
            store.append_event(run_id, {"type": "infrastructure_error", "error_type": type(exc).__name__})
        if agent_result and agent_result.get("billing_unknown"):
            result["billing_uncertain"] = True
        store.finish_run(run_id, result)
        spent += result.get("estimated_cost_usd", 0)
        completed += 1
        print(f"Finished {run_id}: {result['status']}; recorded API estimate ${spent:.2f}", flush=True)
        if result.get("billing_uncertain") or (limit is not None and completed >= limit):
            break
    return {"runs_finished": completed, "estimated_cost_usd": spent}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("estimate", "plan"):
        sub = commands.add_parser(name)
        sub.add_argument("--model", action="append", dest="models")
        sub.add_argument("--task", action="append", choices=TASKS)
        sub.add_argument("--language", action="append", choices=LANGUAGES)
        sub.add_argument("--repetitions", type=int, default=3)
        if name == "plan":
            sub.add_argument("--results", type=Path, required=True)
            sub.add_argument("--image", default=DEFAULT_IMAGE)
            sub.add_argument("--seed", type=int, default=1729)
            sub.add_argument("--max-turns", type=int, default=100)
            sub.add_argument("--wall-seconds", type=float, default=1800)
            sub.add_argument("--per-run-usd", type=float, default=20)
        else:
            sub.add_argument("--json", action="store_true")
    sub = commands.add_parser("run")
    sub.add_argument("--results", type=Path, required=True)
    sub.add_argument("--budget-usd", type=float, required=True)
    sub.add_argument("--limit", type=int)
    sub = commands.add_parser("status")
    sub.add_argument("--results", type=Path, required=True)
    commands.add_parser("doctor", help="inspect installed native CLI capabilities without inference")
    args = parser.parse_args(argv)
    try:
        if args.command == "estimate":
            report = estimate(select_models(args.models), len(args.task or TASKS),
                              len(args.language or LANGUAGES), args.repetitions)
            print(json.dumps(report, indent=2) if args.json else format_estimate(report))
        elif args.command == "plan":
            plan = make_plan(args.results, args.models, args.task or TASKS, args.language or LANGUAGES,
                             args.repetitions, args.seed, args.image, args.max_turns, args.wall_seconds,
                             args.per_run_usd)
            print(format_estimate(plan["cost_scenarios"]))
            print(f"Saved frozen plan in {args.results}; no model calls made.")
        elif args.command == "run":
            print(json.dumps(execute_plan(args.results, args.budget_usd, args.limit), indent=2))
        elif args.command == "status":
            print(json.dumps(ResultStore(args.results).summary(), indent=2))
        else:
            from experiments.native import native_doctor
            print(json.dumps(native_doctor(), indent=2))
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        parser.exit(2, f"Experiment error: {exc}\n")


if __name__ == "__main__":
    main()
