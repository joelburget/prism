#!/usr/bin/env python3
"""Dependency-free, black-box acceptance runner for the three change pilots."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any


ROOT = Path(__file__).resolve().parent
TASKS = ("query-null", "workflow-recovery", "ledger-refunds")
PHASES = ("baseline", "extension")
MAX_OUTPUT_BYTES = 1024 * 1024


class ContractError(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _reject_number(value: str) -> None:
    raise ContractError(f"only JSON integer numbers are supported, got {value}")


def parse_json(raw: str) -> Any:
    """Reject ambiguous JSON, non-finite values, and floating point numbers."""
    try:
        return json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_float=_reject_number,
            parse_constant=_reject_number,
        )
    except (ValueError, RecursionError) as exc:
        raise ContractError(str(exc)) from exc


def response_contract(value: Any) -> None:
    if not isinstance(value, dict) or type(value.get("ok")) is not bool:
        raise ContractError("response must be an object with a Boolean 'ok'")
    if value["ok"]:
        if set(value) != {"ok", "result"}:
            raise ContractError("success response must contain exactly 'ok' and 'result'")
    else:
        if set(value) != {"ok", "error"}:
            raise ContractError("error response must contain exactly 'ok' and 'error'")
        error = value["error"]
        if not isinstance(error, dict) or set(error) != {"code"}:
            raise ContractError("error must contain exactly 'code'")
        if not isinstance(error["code"], str) or not error["code"]:
            raise ContractError("error code must be a nonempty string")


@dataclass(frozen=True)
class Case:
    task: str
    id: str
    phase: str
    description: str
    input: dict[str, Any]
    expect: dict[str, Any]

    @property
    def name(self) -> str:
        return f"{self.task}/{self.id}"

    @property
    def request(self) -> dict[str, Any]:
        return {"protocol_version": 1, "task": self.task, "input": self.input}


def load_cases(root: Path = ROOT, tasks: list[str] | tuple[str, ...] = TASKS) -> list[Case]:
    cases: list[Case] = []
    for task in TASKS:
        if task not in tasks:
            continue
        path = root / task / "cases.json"
        try:
            doc = parse_json(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ContractError) as exc:
            raise ContractError(f"{path}: {exc}") from exc
        if not isinstance(doc, dict) or set(doc) != {"schema_version", "task", "cases"}:
            raise ContractError(f"{path}: expected schema_version, task, cases")
        if type(doc["schema_version"]) is not int or doc["schema_version"] != 1:
            raise ContractError(f"{path}: unsupported schema_version")
        if doc["task"] != task or not isinstance(doc["cases"], list) or not doc["cases"]:
            raise ContractError(f"{path}: incorrect task or empty/non-array cases")
        ids: set[str] = set()
        phases: set[str] = set()
        for index, record in enumerate(doc["cases"]):
            prefix = f"{path}: case {index}"
            keys = {"id", "phase", "description", "input", "expect"}
            if not isinstance(record, dict) or set(record) != keys:
                raise ContractError(f"{prefix}: expected exactly {sorted(keys)}")
            name = record["id"]
            if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
                raise ContractError(f"{prefix}: invalid id")
            if name in ids:
                raise ContractError(f"{prefix}: duplicate id {name}")
            ids.add(name)
            if record["phase"] not in PHASES:
                raise ContractError(f"{prefix}: invalid phase")
            phases.add(record["phase"])
            if not isinstance(record["description"], str) or not record["description"].strip():
                raise ContractError(f"{prefix}: missing description")
            if not isinstance(record["input"], dict):
                raise ContractError(f"{prefix}: input must be an object")
            try:
                response_contract(record["expect"])
            except ContractError as exc:
                raise ContractError(f"{prefix}: {exc}") from exc
            cases.append(Case(task=task, **record))
        if phases != set(PHASES):
            raise ContractError(f"{path}: must contain baseline and extension cases")
    return cases


def corpus_digest(cases: list[Case]) -> str:
    """Fingerprint selected cases, including expected outputs, independent of formatting."""
    encoded = json.dumps([asdict(case) for case in cases], sort_keys=True,
                         ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def first_difference(expected: Any, actual: Any, path: str = "$") -> str | None:
    # Python considers True == 1; the protocol deliberately does not.
    if type(expected) is not type(actual):
        return f"{path}: expected {type(expected).__name__}, got {type(actual).__name__}"
    if isinstance(expected, dict):
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        if missing or extra:
            return f"{path}: missing keys {missing}, extra keys {extra}"
        for key in expected:
            difference = first_difference(expected[key], actual[key], f"{path}[{json.dumps(key)}]")
            if difference:
                return difference
    elif isinstance(expected, list):
        if len(expected) != len(actual):
            return f"{path}: expected {len(expected)} items, got {len(actual)}"
        for index, (left, right) in enumerate(zip(expected, actual)):
            difference = first_difference(left, right, f"{path}[{index}]")
            if difference:
                return difference
    elif expected != actual:
        return f"{path}: expected {_preview(expected)}, got {_preview(actual)}"
    return None


def _preview(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True)[:240]


def _stop(process: subprocess.Popen[bytes]) -> None:
    # Also stop descendants, including those retaining stdout after the parent exits.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def invoke(
    command: list[str], request: dict[str, Any], timeout: float,
    cwd: str | None = None, max_output: int = MAX_OUTPUT_BYTES,
) -> dict[str, Any]:
    """Bound time and captured output without shell evaluation or pipe deadlocks."""
    started = time.monotonic()
    encoded = (json.dumps(request, ensure_ascii=True, separators=(",", ":")) + "\n").encode("utf-8")
    try:
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=cwd, start_new_session=True,
        )
    except OSError as exc:
        return {"status": "error", "reason": f"could not launch command: {exc}", "elapsed_seconds": 0.0}

    output = {"stdout": bytearray(), "stderr": bytearray()}
    failure = None
    position = 0
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    with selectors.DefaultSelector() as selector:
        for stream, kind, event in (
            (process.stdin, "stdin", selectors.EVENT_WRITE),
            (process.stdout, "stdout", selectors.EVENT_READ),
            (process.stderr, "stderr", selectors.EVENT_READ),
        ):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, event, kind)
        try:
            while selector.get_map() or process.poll() is None:
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    failure = f"timeout after {timeout:g}s"
                    break
                for key, _ in selector.select(min(remaining, 0.05)):
                    stream, kind = key.fileobj, key.data
                    if kind == "stdin":
                        try:
                            position += os.write(stream.fileno(), encoded[position:position + 65536])
                        except BrokenPipeError:
                            position = len(encoded)
                        except BlockingIOError:
                            continue
                        if position == len(encoded):
                            selector.unregister(stream)
                            stream.close()
                    else:
                        try:
                            chunk = os.read(stream.fileno(), 65536)
                        except BlockingIOError:
                            continue
                        if not chunk:
                            selector.unregister(stream)
                            stream.close()
                        elif len(output[kind]) + len(chunk) > max_output:
                            output[kind].extend(chunk[:max(0, max_output - len(output[kind]))])
                            failure = f"{kind} exceeded {max_output} bytes"
                            break
                        else:
                            output[kind].extend(chunk)
                if failure:
                    break
        finally:
            # This is also needed if a command exits with descendants still running.
            _stop(process)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()

    result: dict[str, Any] = {
        "status": "fail" if failure else "received",
        "elapsed_seconds": round(time.monotonic() - started, 6),
        "exit_code": process.returncode,
        "stderr": bytes(output["stderr"]).decode("utf-8", errors="replace"),
    }
    if failure:
        result["reason"] = failure
        return result
    if process.returncode != 0:
        result.update(status="fail", reason=f"process exited with status {process.returncode}")
        return result
    try:
        actual = parse_json(bytes(output["stdout"]).decode("utf-8"))
        response_contract(actual)
    except (UnicodeError, ContractError) as exc:
        result.update(status="fail", reason=f"invalid response: {exc}")
    else:
        result["actual"] = actual
    return result


def run_case(case: Case, command: list[str], timeout: float, cwd: str | None) -> dict[str, Any]:
    result = invoke(command, case.request, timeout, cwd)
    result.update(task=case.task, id=case.id, phase=case.phase)
    if result["status"] == "received":
        difference = first_difference(case.expect, result["actual"])
        result["status"] = "fail" if difference else "pass"
        if difference:
            result["reason"] = difference
            result["expected"] = case.expect
    return result


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
            temporary = file.name
            json.dump(report, file, indent=2, ensure_ascii=True)
            file.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def positive_timeout(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("timeout must be positive and finite")
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    for action in ("validate", "list", "run"):
        sub = subparsers.add_parser(action)
        sub.add_argument("--task", action="append", choices=TASKS, help="repeat to select multiple tasks")
        sub.add_argument("--phase", choices=(*PHASES, "all"), default="all")
        sub.add_argument("--corpus-root", type=Path, default=ROOT,
                         help="directory containing TASK/cases.json (default: public suite beside run.py)")
        if action == "run":
            sub.add_argument("--command", required=True, help="executable and arguments, split with shlex; no shell")
            sub.add_argument("--cwd", help="working directory for the implementation")
            sub.add_argument("--timeout", type=positive_timeout, default=10.0, help="seconds per case (default: 10)")
            sub.add_argument("--report", type=Path, help="write a detailed JSON report")
            sub.add_argument("--case", action="append", help="select case IDs (repeatable; exact match)")
    args = parser.parse_args(argv)
    try:
        all_cases = load_cases(args.corpus_root, args.task or TASKS)
    except ContractError as exc:
        print(f"Invalid corpus: {exc}", file=sys.stderr)
        return 2
    cases = [case for case in all_cases if (not args.task or case.task in args.task)
             and (args.phase == "all" or case.phase == args.phase)]
    if args.action == "run" and args.case:
        missing = set(args.case) - {case.id for case in cases}
        if missing:
            parser.error(f"case IDs not found in selection: {', '.join(sorted(missing))}")
        cases = [case for case in cases if case.id in args.case]
    if not cases:
        parser.error("selection contains no cases")
    if args.action == "list":
        for case in cases:
            print(f"{case.name}\t{case.phase}\t{case.description}")
        return 0
    if args.action == "validate":
        for task in TASKS:
            counts = Counter(case.phase for case in cases if case.task == task)
            if counts:
                print(f"{task}: " + ", ".join(f"{counts[phase]} {phase}" for phase in PHASES))
        print(f"Validated {len(all_cases)} cases; {len(cases)} selected. (Structure only; no implementation executed.)")
        print(f"Selected corpus SHA-256: {corpus_digest(cases)}")
        return 0
    try:
        command = shlex.split(args.command)
    except ValueError as exc:
        parser.error(str(exc))
    if not command:
        parser.error("--command must not be empty")
    results = []
    for case in cases:
        result = run_case(case, command, args.timeout, args.cwd)
        results.append(result)
        detail = f": {result['reason']}" if "reason" in result else ""
        print(f"{result['status'].upper()} {case.name}{detail}", flush=True)
        if result["status"] == "error":
            break
    summary = {}
    for task in TASKS:
        for phase in PHASES:
            selected = [case for case in cases if case.task == task and case.phase == phase]
            if not selected:
                continue
            counts = Counter(result["status"] for result in results
                             if result["task"] == task and result["phase"] == phase)
            group = {"selected": len(selected), "pass": counts["pass"], "fail": counts["fail"],
                     "error": counts["error"], "not_run": len(selected) - sum(counts.values())}
            summary[f"{task}/{phase}"] = group
            print(f"{task}/{phase}: {group['pass']}/{group['selected']} passed, "
                  f"{group['fail']} failed, {group['error']} errors, {group['not_run']} not run")
    if args.report:
        try:
            write_report(args.report, {"schema_version": 1, "command": command,
                                      "cwd": str(Path(args.cwd or '.').resolve()),
                                      "corpus_root": str(args.corpus_root.resolve()),
                                      "corpus_sha256": corpus_digest(cases),
                                      "timeout_seconds": args.timeout, "summary": summary, "cases": results})
        except OSError as exc:
            print(f"Could not write report: {exc}", file=sys.stderr)
            return 2
    if any(result["status"] == "error" for result in results):
        return 2
    return 1 if any(result["status"] != "pass" for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
