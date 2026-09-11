"""Evaluator-only run records. JSON is authoritative; SQLite is a disposable index."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import sqlite3
import tempfile
import threading
from contextlib import closing
from datetime import datetime, timezone


def _now():
    return datetime.now(timezone.utc).isoformat()


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write_once(path, value):
    """Publish a complete file atomically without replacing an existing record."""
    data = (_json(value) + "\n").encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=".record-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o400)
        os.link(temporary, path)
    finally:
        os.unlink(temporary)


class ResultStore:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        repository = Path(__file__).resolve().parents[3]
        if self.root == repository or repository in self.root.parents:
            raise ValueError("results root must be outside the evaluator repository")
        if any((parent / ".git").exists() for parent in (self.root, *self.root.parents)):
            raise ValueError("results root must be outside Git working trees")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.runs = self.root / "runs"
        if self.runs.is_symlink():
            raise ValueError("runs directory cannot be a symlink")
        self.runs.mkdir(mode=0o700, exist_ok=True)
        self.index_path = self.root / "index.sqlite3"
        if self.index_path.is_symlink():
            raise ValueError("index cannot be a symlink")
        self._lock = threading.RLock()
        self.rebuild_index()

    def run_dir(self, run_id):
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", run_id):
            raise ValueError("invalid run ID")
        path = self.runs / run_id
        if path.is_symlink():
            raise ValueError("run directory cannot be a symlink")
        return path

    def artifact_path(self, run_id, reference):
        """Resolve a relative artifact reference without permitting traversal or symlinks."""
        run = self.run_dir(run_id)
        relative = Path(reference)
        if relative.is_absolute() or not relative.parts or any(p in ("..", ".") for p in relative.parts):
            raise ValueError("artifact must be a relative path within the run")
        target = run / relative
        if any(p.is_symlink() for p in (target, *target.parents) if p != self.root):
            raise ValueError("artifact cannot use symlinks")
        if not target.resolve().is_relative_to(run.resolve()):
            raise ValueError("artifact escapes run directory")
        return target

    def write_artifact(self, run_id, reference, content):
        path = self.artifact_path(run_id, reference)
        if not (self.run_dir(run_id) / "metadata.json").exists():
            raise ValueError("unknown run")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with path.open("xb") as stream:
            stream.write(content.encode("utf-8") if isinstance(content, str) else content)
        path.chmod(0o400)
        return str(path.relative_to(self.run_dir(run_id)))

    def create_run(self, run_id, metadata):
        if not isinstance(metadata, dict):
            raise ValueError("metadata must be an object")
        _json(metadata)
        with self._lock:
            directory = self.run_dir(run_id)
            directory.mkdir(mode=0o700)
            _write_once(directory / "metadata.json", {**metadata, "run_id": run_id,
                                                       "created_at": metadata.get("created_at", _now())})
            (directory / "events.jsonl").touch(mode=0o600)
            self.rebuild_index()
        return directory

    def append_event(self, run_id, event):
        if not isinstance(event, dict):
            raise ValueError("event must be an object")
        with self._lock:
            directory = self.run_dir(run_id)
            if not (directory / "metadata.json").is_file():
                raise ValueError("unknown run")
            record = {**event, "recorded_at": event.get("recorded_at", _now())}
            with self.artifact_path(run_id, "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(_json(record) + "\n")
                stream.flush()
                os.fsync(stream.fileno())

    def finish_run(self, run_id, result):
        if not isinstance(result, dict):
            raise ValueError("result must be an object")
        with self._lock:
            self._metadata(run_id)
            _write_once(self.artifact_path(run_id, "result.json"),
                        {**result, "finished_at": result.get("finished_at", _now())})
            self.rebuild_index()

    def _metadata(self, run_id):
        return _read(self.artifact_path(run_id, "metadata.json"))

    @staticmethod
    def validate_review(review):
        if not isinstance(review, dict):
            raise ValueError("review must be an object")
        for key in ("reviewer", "comprehension_answer"):
            if not isinstance(review.get(key), str) or not review[key].strip():
                raise ValueError(f"{key} is required")
        if review.get("decision") not in ("accept", "request_changes"):
            raise ValueError("decision must be accept or request_changes")
        elapsed = review.get("active_elapsed_seconds")
        if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed) or elapsed < 0:
            raise ValueError("active_elapsed_seconds must be a finite nonnegative number")
        confidence = review.get("confidence")
        if type(confidence) is not int or not 1 <= confidence <= 5:
            raise ValueError("confidence must be an integer from 1 to 5")
        findings = review.get("findings")
        if not isinstance(findings, list):
            raise ValueError("findings must be a list")
        if review["decision"] == "request_changes" and not findings:
            raise ValueError("request_changes requires a concrete finding")
        for finding in findings:
            if not isinstance(finding, dict) or finding.get("severity") not in ("critical", "high", "medium", "low"):
                raise ValueError("finding severity must be critical, high, medium, or low")
            for key in ("location", "text"):
                if not isinstance(finding.get(key), str) or not finding[key].strip():
                    raise ValueError(f"finding {key} is required")

    def save_review(self, run_id, review):
        self.validate_review(review)
        # Keep the human measurement separate from metadata and correctness results.
        clean = {key: review[key] for key in ("reviewer", "decision", "active_elapsed_seconds",
                                              "findings", "comprehension_answer", "confidence")}
        with self._lock:
            if not self.artifact_path(run_id, "result.json").is_file():
                raise ValueError("run must be finished before review")
            _write_once(self.artifact_path(run_id, "review.json"), {**clean, "recorded_at": _now()})
            self.rebuild_index()
        return _read(self.artifact_path(run_id, "review.json"))

    def get_run(self, run_id, blind=True):
        metadata = self._metadata(run_id)
        result_path = self.artifact_path(run_id, "result.json")
        review_path = self.artifact_path(run_id, "review.json")
        view = {"run_id": run_id, "task": metadata.get("task"), "language": metadata.get("language"),
                "finished": result_path.is_file(), "reviewed": review_path.is_file(),
                "comprehension_prompt": metadata.get("comprehension_prompt", "Explain the changed behavior and one edge case the implementation handles.")}
        if not blind:
            view.update(metadata=metadata, result=_read(result_path) if result_path.is_file() else None,
                        review=_read(review_path) if review_path.is_file() else None)
        return view

    def source_diff(self, run_id):
        result_path = self.artifact_path(run_id, "result.json")
        result = _read(result_path) if result_path.is_file() else {}
        reference = result.get("patch_path") or result.get("source_patch") or result.get("patch") or "source.patch"
        if isinstance(reference, dict):
            reference = reference.get("path", "source.patch")
        path = self.artifact_path(run_id, reference)
        return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else "No source diff recorded."

    def review_context(self, run_id, view=None, filename=None):
        """Allowlisted public task and frozen before/after sources, never arbitrary artifacts."""
        self._metadata(run_id)
        files = []
        for label, folder in (("before", "baseline"), ("after", "source")):
            directory = self.artifact_path(run_id, folder)
            if not directory.is_dir():
                continue
            for base, directories, names in os.walk(directory, followlinks=False):
                directories[:] = sorted(name for name in directories if not (Path(base) / name).is_symlink())
                for name in sorted(names):
                    path = Path(base) / name
                    if path.is_file() and not path.is_symlink():
                        files.append({"view": label, "file": path.relative_to(directory).as_posix()})
        specification = self.artifact_path(run_id, "problem.md")
        if view is None:
            return {"has_spec": specification.is_file(), "files": files}
        if view == "spec" and filename is None and specification.is_file():
            path = specification
        elif {"view": view, "file": filename} in files:
            folder = "baseline" if view == "before" else "source"
            path = self.artifact_path(run_id, f"{folder}/{filename}")
        else:
            raise ValueError("unknown review context file")
        return {"view": view, "file": filename, "text": path.read_text(encoding="utf-8", errors="replace")}

    def reveal_run(self, run_id):
        if not self.artifact_path(run_id, "review.json").is_file():
            raise ValueError("record review before revealing model identities or correctness")
        self.append_event(run_id, {"type": "review_reveal"})
        return self.get_run(run_id, blind=False)

    def list_runs(self, seed=0, subset=None):
        selected = set(subset) if subset is not None else None
        ids = sorted(path.name for path in self.runs.iterdir() if path.is_dir() and not path.is_symlink()
                     and (path / "metadata.json").is_file())
        if selected is not None:
            missing = selected.difference(ids)
            if missing:
                raise ValueError("unknown runs in subset: " + ", ".join(sorted(missing)))
            ids = [run_id for run_id in ids if run_id in selected]
        random.Random(str(seed)).shuffle(ids)
        return [self.get_run(run_id) for run_id in ids]

    def rebuild_index(self):
        with self._lock, closing(sqlite3.connect(self.index_path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DROP TABLE IF EXISTS runs")
            db.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY, task TEXT, language TEXT, model TEXT, harness TEXT, status TEXT, reviewed INTEGER, success INTEGER, public_pass INTEGER, heldout_pass INTEGER, elapsed_seconds REAL, estimated_cost_usd REAL, observed_cost_usd REAL, review_seconds REAL)")
            for directory in sorted(self.runs.iterdir()):
                if not directory.is_dir() or directory.is_symlink() or not (directory / "metadata.json").is_file():
                    continue
                metadata = self._metadata(directory.name)
                result_path = self.artifact_path(directory.name, "result.json")
                result = _read(result_path) if result_path.is_file() else {}
                model = metadata.get("model", metadata.get("model_id"))
                if isinstance(model, dict):
                    model = model.get("key") or model.get("model_id") or _json(model)
                review_path = self.artifact_path(directory.name, "review.json")
                review = _read(review_path) if review_path.is_file() else {}
                scores = result.get("scores", {})
                def corpus_pass(name):
                    value = scores.get(name, {}).get("passed")
                    return int(value) if isinstance(value, bool) else None
                db.execute("INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (directory.name, metadata.get("task"), metadata.get("language"),
                            _json(model) if isinstance(model, (dict, list)) else model,
                            metadata.get("harness", "unspecified"),
                            result.get("status", "finished" if result_path.is_file() else "running"),
                            int(review_path.is_file()), int(result["success"]) if isinstance(result.get("success"), bool) else None,
                            corpus_pass("public"), corpus_pass("heldout"), result.get("elapsed_seconds"),
                            result.get("estimated_cost_usd"), result.get("observed_cost_usd"), review.get("active_elapsed_seconds")))
        self.index_path.chmod(0o600)

    def summary(self):
        """Evaluator-only aggregate index; never expose it in the blind review API."""
        self.rebuild_index()
        with closing(sqlite3.connect(self.index_path)) as db:
            db.row_factory = sqlite3.Row
            rows = [dict(row) for row in db.execute("""
                SELECT harness, model, task, language, COUNT(*) AS runs,
                    SUM(status = 'completed') AS completed,
                    SUM(status = 'infrastructure_error') AS infrastructure_errors,
                    SUM(status NOT IN ('completed', 'infrastructure_error')) AS other_status,
                    SUM(reviewed) AS reviewed, SUM(CASE WHEN status = 'completed' THEN COALESCE(success, 0) ELSE 0 END) AS successful,
                    SUM(public_pass IS NOT NULL) AS public_scored, SUM(COALESCE(public_pass, 0)) AS public_passed,
                    SUM(heldout_pass IS NOT NULL) AS heldout_scored, SUM(COALESCE(heldout_pass, 0)) AS heldout_passed,
                    AVG(elapsed_seconds) AS mean_elapsed_seconds,
                    SUM(estimated_cost_usd) AS estimated_cost_usd,
                    SUM(observed_cost_usd) AS observed_cost_usd,
                    AVG(review_seconds) AS mean_review_seconds
                FROM runs GROUP BY harness, model, task, language ORDER BY harness, model, task, language
            """)]
        for row in rows:
            row["success_rate_completed"] = row["successful"] / row["completed"] if row["completed"] else None
        return {"runs": sum(row["runs"] for row in rows), "reviewed": sum(row["reviewed"] for row in rows), "groups": rows}


def anonymous_id(run_id):
    return "run-" + hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:16]
