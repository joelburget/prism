"""Frozen, serial subscription-native programming-agent experiments.

No API client or API key is used. Each cell gets a fresh native CLI container and
an independent, network-disabled task container. Existing run directories are
never retried. Infrastructure failures stop the batch and are not ability scores.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
from pathlib import Path
import random
import subprocess
import tempfile
import time
import uuid

from .control import (COMPREHENSION, HERE, TASKS, LANGUAGES,
                      _image_id, export_public, file_hash, grade_submission,
                      inputs_fingerprint, json_bytes, load_starter, prompt_for,
                      select_models, source_patch, tree_fingerprint)
from .native_config import client_argv
from .native_session import run_client, subscription_status
from .native_setup import DEFAULT_IMAGE as CLIENT_IMAGE, create_client, inspect_boundary
from .native_task_setup import DEFAULT_IMAGE as TASK_IMAGE
from .results import ResultStore
from .sandbox import DockerSandbox, InvalidSubmissionError, SandboxFrozenError

HARNESS = "subscription-native-v2"
VERIFICATION = HERE / "NATIVE_VERIFICATION.json"
SCORED_STOPS = {"completed", "native_wall_timeout", "native_turn_limit", "native_tool_limit", "native_output_limit"}


def effort_for(model):
    return model.get("reasoning") if model["provider"] == "openai" else model.get("effort")


def native_prompt_for(task, language):
    return prompt_for(task, language) + """
The execute tool runs in a writable task container: /work and /work/starter are writable.
The apply_patch command is installed there. To edit files, invoke apply_patch with a patch
on stdin through execute (a shell heredoc is supported). The native client's own filesystem
is separate from the task container. All task inspection, edits, builds and tests use execute.
"""


def checked_verification(path, image_id, models, task_image_id=None):
    report = json.loads(Path(path).read_text())
    if report.get("image_id") != image_id:
        raise ValueError("native image does not match the verification report")
    if task_image_id is not None and report.get("task_image_id") != task_image_id:
        raise ValueError("task image does not match the native tool verification report")
    hashes = report.get("source_sha256", {})
    required = {"native_bridge.py", "native_config.py", "native_probe.py", "native_proxy.py",
                "native_relay.py", "native_session.py", "native_setup.py"}
    if not required <= set(hashes):
        raise ValueError("native verification is missing source fingerprints")
    for name, digest in hashes.items():
        if Path(name).name != name or file_hash(HERE / name) != digest:
            raise ValueError("native verification source changed; reverify before planning")
    for model in models:
        client = "codex" if model["provider"] == "openai" else "claude"
        checks = [c for c in report.get("model_checks", [])
                  if c.get("client") == client and c.get("model_id") == model["model_id"]
                  and c.get("effort") == effort_for(model)]
        if len(checks) != 1 or not checks[0].get("verified") or not checks[0].get("canary_verified") or checks[0].get("unexpected_tools"):
            raise ValueError("native model/effort does not have a passing routing verification")
        boundaries = [c for c in report.get("boundary_checks", []) if c.get("provider") == model["provider"]]
        if len(boundaries) != 1 or not boundaries[0].get("container", {}).get("passed") or not boundaries[0].get("network", {}).get("passed"):
            raise ValueError("native provider boundary has not passed verification")
    return report


def make_plan(root, model_keys=None, tasks=("ledger-refunds",), languages=LANGUAGES,
              repetitions=1, seed=1729, task_image=TASK_IMAGE, client_image=CLIENT_IMAGE,
              max_calls=100, wall_seconds=1800, max_turns=100, verification=VERIFICATION):
    for value in (max_calls, max_turns, repetitions):
        if type(value) is not int or value < 1:
            raise ValueError("repetitions, calls and turns must be positive integers")
    if isinstance(wall_seconds, bool) or not math.isfinite(wall_seconds) or wall_seconds <= 0:
        raise ValueError("wall seconds must be finite and positive")
    for chosen, allowed in ((tasks, TASKS), (languages, LANGUAGES)):
        if not chosen or len(set(chosen)) != len(chosen) or set(chosen) - set(allowed):
            raise ValueError("invalid or duplicate task/language selection")
    store = ResultStore(root)
    if (store.root / "plan.json").exists():
        raise ValueError("plan already exists; use a fresh results directory")
    models = select_models(model_keys)
    task_id, client_id = _image_id(task_image), _image_id(client_image)
    report = checked_verification(verification, client_id, models, task_id)
    cells = [{"run_id": "run-" + uuid.uuid4().hex[:12], "task": task, "language": language,
              "model": model, "effort": effort_for(model), "repetition": repetition,
              "starter_sha256": load_starter(task, language).digest(),
              "prompt": native_prompt_for(task, language)}
             for task in tasks for language in languages for model in models
             for repetition in range(1, repetitions + 1)]
    random.Random(seed).shuffle(cells)
    plan = {"schema_version": 1, "harness": HARNESS, "created_at": datetime.now(timezone.utc).isoformat(),
            "repository_revision": subprocess.run(["git", "-C", str(HERE), "rev-parse", "HEAD"],
                capture_output=True, text=True, check=True).stdout.strip(),
            "inputs": inputs_fingerprint(), "order_seed": seed, "runs": cells,
            "task_image": task_image, "task_image_id": task_id,
            "native_image": client_image, "native_image_id": client_id,
            "verification_sha256": hashlib.sha256(json_bytes(report)).hexdigest(),
            "budgets": {"max_tool_calls": max_calls, "wall_timeout_seconds": wall_seconds,
                        "claude_max_turns": max_turns},
            "accounting": {"route": "subscription", "api_fallback": False,
                           "enforceable_token_cap": None, "enforceable_dollar_cap": None},
            "notes": ["Native client system prompts, compaction and accounting differ from shared-api-v1.",
                      "Explicit model IDs; no fallback model requested; observed mismatches stop the batch.",
                      "Server model identity may not be exposed by the CLI and is recorded as unverified.",
                      "Subscription quota is consumed; CLI dollar reports are API-equivalent estimates.",
                      "No automatic retry of interrupted or failed cells."]}
    for name, value in (("NATIVE_VERIFICATION.json", report), ("plan.json", plan)):
        with (store.root / name).open("xb") as stream:
            stream.write(json_bytes(value))
        (store.root / name).chmod(0o400)
    return plan


def observed_models(event):
    """Read only explicit model metadata; never infer identity from model prose."""
    if event.get("event") != "native_event":
        return set()
    payload = event.get("payload", {})
    if not isinstance(payload, dict):
        return set()
    values = []
    if payload.get("type") == "system":
        values.append(payload.get("model"))
    if isinstance(payload.get("message"), dict):
        values.append(payload["message"].get("model"))
    if isinstance(payload.get("modelUsage"), dict):
        values.extend(payload["modelUsage"])
    return {value for value in values if isinstance(value, str) and value and value != "<synthetic>"}


def execute_plan(root, limit=None):
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError("limit must be a positive integer")
    store = ResultStore(root)
    with (store.root / "execution.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("this experiment already has a running controller")
        return _execute_locked(store, limit)


def _execute_locked(store, limit):
    plan = json.loads((store.root / "plan.json").read_text())
    if plan.get("harness") != HARNESS or plan["inputs"] != inputs_fingerprint():
        raise ValueError("native experiment inputs changed; create a new experiment")
    for key in ("task_image_id", "native_image_id"):
        if _image_id(plan[key]) != plan[key]:
            raise ValueError("pinned experiment image unavailable")
    verification = store.root / "NATIVE_VERIFICATION.json"
    if file_hash(verification) != plan["verification_sha256"]:
        raise ValueError("native verification changed after planning")
    checked_verification(verification, plan["native_image_id"], [c["model"] for c in plan["runs"]], plan["task_image_id"])
    for cell in plan["runs"]:
        directory = store.run_dir(cell["run_id"])
        if directory.exists():
            result_file = directory / "result.json"
            if not result_file.exists():
                raise ValueError("interrupted native rollout must be reconciled; it will not be retried")
            if json.loads(result_file.read_text()).get("status") != "completed":
                raise ValueError("a prior infrastructure failure stops this frozen experiment; create a separate continuation plan")
    finished = 0
    stopped = None
    for cell in plan["runs"]:
        if (store.root / "STOP_AFTER_CURRENT").exists():
            return {"runs_finished": finished, "stopped_on_infrastructure_run": None,
                    "paused_by_operator": True, "harness": HARNESS, "estimated_cost_usd": None}
        run_id, model = cell["run_id"], cell["model"]
        if store.run_dir(run_id).exists():
            continue
        starter = load_starter(cell["task"], cell["language"])
        if starter.digest() != cell["starter_sha256"]:
            raise ValueError("starter changed since planning")
        metadata = {**cell, "harness": HARNESS, "image_id": plan["task_image_id"],
                    "native_image_id": plan["native_image_id"], "budgets": plan["budgets"],
                    "plan_sha256": file_hash(store.root / "plan.json"),
                    "comprehension_prompt": COMPREHENSION[cell["task"]],
                    "automatic_fallback_requested": False, "api_fallback": False}
        store.create_run(run_id, metadata)
        print(f"Starting {run_id}: {cell['task']}/{cell['language']}/{model['key']}", flush=True)
        started, agent_result, seen, tool_failed = time.monotonic(), {}, set(), []
        native_turn_limit = []
        tool_resource_limits = []
        result = None
        def record(event):
            seen.update(observed_models(event))
            payload = event.get("payload", {})
            if isinstance(payload, dict) and payload.get("type") == "result" and payload.get("subtype") == "error_max_turns":
                native_turn_limit.append(True)
            store.append_event(run_id, event)
        try:
            with tempfile.TemporaryDirectory(prefix="prism-native-rollout-") as temporary:
                bundle = export_public(cell["task"], Path(temporary) / "bundle", language=cell["language"])
                store.write_artifact(run_id, "input-manifest.json", (bundle / "MANIFEST.json").read_bytes())
                store.write_artifact(run_id, "problem.md", (bundle / cell["task"] / "PROBLEM.md").read_bytes())
                for original in (bundle / "starter").rglob("*"):
                    if original.is_file():
                        store.write_artifact(run_id, "baseline/" + str(original.relative_to(bundle / "starter")), original.read_bytes())
                with DockerSandbox(plan["task_image_id"], bundle) as sandbox:
                    def execute(command, timeout_seconds):
                        if seen - {model["model_id"]}:
                            raise RuntimeError("native model identity mismatch")
                        try:
                            output = sandbox.execute(command, timeout_seconds)
                            if output.get("timed_out") or output.get("output_limited"):
                                tool_resource_limits.append("timeout" if output.get("timed_out") else "output_limit")
                            return output
                        except SandboxFrozenError:
                            tool_resource_limits.append("task_frozen_after_resource_limit")
                            raise
                        except Exception:
                            tool_failed.append(True)
                            raise
                    with create_client(model["provider"], plan["native_image_id"]) as client:
                        boundary = inspect_boundary(client)
                        if not boundary.get("passed"):
                            raise RuntimeError("native boundary failed")
                        auth = subscription_status(client)
                        store.write_artifact(run_id, "native-environment.json", json_bytes({"boundary": boundary, "auth": auth}))
                        if not auth.get("authenticated"):
                            raise RuntimeError("native subscription authentication required")
                        argv = client_argv(model["provider"], model["model_id"], cell["effort"],
                                           max_turns=plan["budgets"]["claude_max_turns"])
                        store.write_artifact(run_id, "native-argv.json", json_bytes(argv))
                        agent_result = run_client(client, argv, cell["prompt"], execute, record,
                            wall_seconds=plan["budgets"]["wall_timeout_seconds"],
                            max_calls=plan["budgets"]["max_tool_calls"])
                        store.write_artifact(run_id, "native-result.json", json_bytes(agent_result))
                    if native_turn_limit and agent_result.get("stop_reason") == "native_client_error":
                        agent_result["stop_reason"] = "native_turn_limit"
                    sandbox.freeze()
                    source = sandbox.snapshot(store.run_dir(run_id) / "source")
                store.write_artifact(run_id, "source.patch", source_patch(bundle / "starter", source))
                infrastructure = (agent_result.get("stop_reason") not in SCORED_STOPS
                                  or bool(seen - {model["model_id"]}) or bool(tool_failed))
                result = {**agent_result, "status": "infrastructure_error" if infrastructure else "completed",
                          "success": None, "source_sha256": tree_fingerprint(source), "patch": "source.patch"}
                if not infrastructure:
                    scores = grade_submission(plan["task_image_id"], source, cell["task"], starter.build)
                    result.update(scores=scores, success=all(report["passed"] for report in scores.values()))
        except InvalidSubmissionError:
            infrastructure = bool(agent_result.get("stop_reason") not in SCORED_STOPS or seen - {model["model_id"]} or tool_failed)
            result = {**agent_result, "status": "infrastructure_error" if infrastructure else "completed",
                      "success": None if infrastructure else False, "invalid_submission": True}
        except Exception as exc:
            result = {**agent_result, "status": "infrastructure_error", "success": None,
                      "error_type": type(exc).__name__}
            store.append_event(run_id, {"event": "infrastructure_error", "error_type": type(exc).__name__})
        result.update(harness=HARNESS, total_elapsed_seconds=time.monotonic() - started,
                      requested_model_id=model["model_id"], observed_model_ids=sorted(seen),
                      tool_resource_limits=sorted(set(tool_resource_limits)),
                      server_model_identity="unverified" if not seen else "mismatch" if seen - {model["model_id"]} else "reported_match",
                      estimated_cost_usd=None, automatic_fallback_requested=False, api_fallback=False)
        store.finish_run(run_id, result)
        finished += 1
        print(f"Finished {run_id}: {result['status']}; {result.get('stop_reason', result.get('error_type'))}", flush=True)
        if result["status"] == "infrastructure_error":
            stopped = run_id
            break
        if limit is not None and finished >= limit:
            break
    return {"runs_finished": finished, "stopped_on_infrastructure_run": stopped, "harness": HARNESS,
            "accounting": "subscription; no API fallback", "estimated_cost_usd": None}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="freeze 24 ledger calibration runs by default; no inference")
    plan.add_argument("--results", type=Path, required=True)
    plan.add_argument("--model", action="append", dest="models")
    plan.add_argument("--task", choices=TASKS, action="append")
    plan.add_argument("--language", choices=LANGUAGES, action="append")
    plan.add_argument("--repetitions", type=int, default=1)
    plan.add_argument("--seed", type=int, default=1729)
    plan.add_argument("--task-image", default=TASK_IMAGE)
    plan.add_argument("--native-image", default=CLIENT_IMAGE)
    plan.add_argument("--max-tool-calls", type=int, default=100)
    plan.add_argument("--max-turns", type=int, default=100)
    plan.add_argument("--wall-seconds", type=float, default=1800)
    run = commands.add_parser("run")
    run.add_argument("--results", type=Path, required=True)
    run.add_argument("--limit", type=int)
    status = commands.add_parser("status")
    status.add_argument("--results", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            value = make_plan(args.results, args.models, args.task or ("ledger-refunds",), args.language or LANGUAGES,
                              args.repetitions, args.seed, args.task_image, args.native_image,
                              args.max_tool_calls, args.wall_seconds, args.max_turns)
            print(f"Saved frozen subscription plan with {len(value['runs'])} runs: {args.results}")
        elif args.command == "run":
            value = execute_plan(args.results, args.limit)
            print(json.dumps(value, indent=2))
            if value["stopped_on_infrastructure_run"]:
                return 1
        else:
            print(json.dumps(ResultStore(args.results).summary(), indent=2))
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        # Exception text could contain client output. Persist only controlled/type information.
        parser.exit(2, f"Native experiment error: {type(exc).__name__}; no automatic retry.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
