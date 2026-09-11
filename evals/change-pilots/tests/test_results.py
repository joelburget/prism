"""Storage integrity and the local blind-review trust boundary; no model calls."""
import http.client
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experiments.results import ResultStore, anonymous_id
from experiments.review import make_server, PAGE


def review():
    return {"reviewer": "reviewer-1", "decision": "request_changes", "active_elapsed_seconds": 42.5,
            "findings": [{"location": "main.py:12", "severity": "high", "text": "Empty input raises an exception."}],
            "comprehension_answer": "The implementation branches on empty input.", "confidence": 4}


class StoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "results"
        self.store = ResultStore(self.root)

    def create(self, name="run1"):
        self.store.create_run(name, {"task": "query-null", "language": "python", "model": "SECRET-MODEL",
                                     "transcript": "SECRET-TRANSCRIPT", "harness": "fake"})
        self.store.write_artifact(name, "source.patch", "+print('<script>alert(1)</script>')\n")
        self.store.finish_run(name, {"status": "completed", "scores": {"heldout": {"passed": 17, "total": 20}},
                                     "patch_path": "source.patch"})

    def test_json_is_authoritative_and_index_is_rebuildable(self):
        self.create()
        self.store.append_event("run1", {"type": "metric", "value": 2})
        self.assertEqual(json.loads((self.store.run_dir("run1") / "events.jsonl").read_text())["value"], 2)
        self.store.index_path.unlink()
        restored = ResultStore(self.root)
        self.assertEqual(restored.summary()["runs"], 1)
        with closing(sqlite3.connect(restored.index_path)) as connection:
            self.assertEqual(connection.execute("SELECT status FROM runs").fetchone()[0], "completed")
        self.assertEqual(restored.get_run("run1", blind=False)["metadata"]["model"], "SECRET-MODEL")

    def test_run_and_finished_records_cannot_be_overwritten(self):
        self.create()
        before = (self.store.run_dir("run1") / "metadata.json").read_bytes()
        with self.assertRaises(FileExistsError):
            self.store.create_run("run1", {"model": "replacement"})
        with self.assertRaises(FileExistsError):
            self.store.finish_run("run1", {"status": "replacement"})
        with self.assertRaises(FileExistsError):
            self.store.write_artifact("run1", "source.patch", "replacement")
        self.assertEqual(before, (self.store.run_dir("run1") / "metadata.json").read_bytes())

    def test_review_before_reveal_and_no_metadata_mutation(self):
        self.create()
        directory = self.store.run_dir("run1")
        frozen = {name: (directory / name).read_bytes() for name in ("metadata.json", "result.json")}
        blind = json.dumps(self.store.get_run("run1"))
        for secret in ("SECRET-MODEL", "SECRET-TRANSCRIPT", "scores", "passed"):
            self.assertNotIn(secret, blind)
        with self.assertRaisesRegex(ValueError, "record review"):
            self.store.reveal_run("run1")
        self.store.save_review("run1", review())
        self.assertEqual(self.store.reveal_run("run1")["metadata"]["model"], "SECRET-MODEL")
        with self.assertRaises(FileExistsError):
            self.store.save_review("run1", review())
        for name, content in frozen.items():
            self.assertEqual(content, (directory / name).read_bytes())
        self.assertTrue((directory / "review.json").is_file())

    def test_review_validation(self):
        self.create()
        bad = [{"reviewer": ""}, {"decision": "approve"}, {"active_elapsed_seconds": -1},
               {"active_elapsed_seconds": True}, {"active_elapsed_seconds": float("nan")},
               {"confidence": 6}, {"confidence": True}, {"comprehension_answer": " "},
               {"findings": []}, {"findings": [{"severity": "high", "location": "", "text": "bad"}]}]
        for change in bad:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.store.save_review("run1", {**review(), **change})
        self.assertFalse((self.store.run_dir("run1") / "review.json").exists())

    def test_seeded_order_and_subset(self):
        for number in range(8):
            self.create(f"run{number}")
        self.assertEqual(self.store.list_runs(seed=7), self.store.list_runs(seed=7))
        self.assertNotEqual(self.store.list_runs(seed=7), self.store.list_runs(seed=8))
        self.assertEqual({run["run_id"] for run in self.store.list_runs(subset=["run1", "run3"])}, {"run1", "run3"})
        with self.assertRaises(ValueError):
            self.store.list_runs(subset=["missing"])

    def test_summary_separates_infrastructure_errors_and_model_groups(self):
        for name, harness, status, success in (("one", "shared-api-v1", "completed", True),
                                               ("two", "shared-api-v1", "infrastructure_error", True),
                                               ("three", "other-harness", "completed", False)):
            self.store.create_run(name, {"task": "query-null", "language": "python", "harness": harness,
                                         "model": {"key": "model-a", "model_id": "provider-version"}})
            result = {"status": status, "success": success}
            if status == "completed":
                result["scores"] = {"public": {"passed": True}, "heldout": {"passed": success}}
            self.store.finish_run(name, result)
        summary = self.store.summary()
        self.assertEqual(summary["runs"], 3)
        self.assertEqual(len(summary["groups"]), 2)
        group = next(row for row in summary["groups"] if row["harness"] == "shared-api-v1")
        self.assertEqual(group["model"], "model-a")
        self.assertEqual(group["completed"], 1)
        self.assertEqual(group["infrastructure_errors"], 1)
        self.assertEqual(group["success_rate_completed"], 1.0)
        self.assertEqual(group["heldout_scored"], 1)

    def test_traversal_symlinks_and_repository_outputs_rejected(self):
        self.create()
        for name in ("../outside", "/tmp/outside", "run/child", ".", ".."):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.store.run_dir(name)
        for reference in ("../outside", "/tmp/outside"):
            with self.assertRaises(ValueError):
                self.store.artifact_path("run1", reference)
        (self.store.run_dir("run1") / "link").symlink_to(self.root.parent, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.store.artifact_path("run1", "link/outside")
        with self.assertRaises(ValueError):
            ResultStore(Path(__file__).resolve().parents[1] / "forbidden-results")


class ReviewHTTPTests(unittest.TestCase):
    def setUp(self):
        StoreTests.setUp(self)
        StoreTests.create(self, "SECRET-MODEL-run")
        self.server = make_server(self.store, seed=13)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.alias = anonymous_id("SECRET-MODEL-run")

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        request_headers = {"X-Review-Token": self.server.review_token, "Content-Type": "application/json"}
        request_headers.update(headers or {})
        connection.request(method, path, body=json.dumps(body) if body is not None else None, headers=request_headers)
        response = connection.getresponse()
        data = response.read().decode()
        status = response.status
        response_headers = dict(response.getheaders())
        connection.close()
        return status, data, response_headers

    def test_http_blind_api_and_explicit_reveal(self):
        for path in ("/api/runs", "/api/run/" + self.alias):
            status, body, _ = self.request("GET", path)
            self.assertEqual(status, 200)
            for secret in ("SECRET-MODEL", "SECRET-TRANSCRIPT", "scores", "heldout", '"passed"'):
                self.assertNotIn(secret, body)
            self.assertIn("python", body)
        status, _, _ = self.request("POST", "/api/reveal/" + self.alias, {})
        self.assertEqual(status, 400)
        status, body, _ = self.request("POST", "/api/review/" + self.alias, review())
        self.assertEqual(status, 200)
        self.assertNotIn("SECRET-MODEL", body)
        status, body, _ = self.request("GET", "/api/run/" + self.alias)
        self.assertNotIn("SECRET-MODEL", body)
        status, body, _ = self.request("POST", "/api/reveal/" + self.alias, {})
        self.assertEqual(status, 200)
        self.assertIn("SECRET-MODEL", body)
        self.assertIn('"passed": 17', body)
        status, _, _ = self.request("POST", "/api/review/" + self.alias, review())
        self.assertEqual(status, 409)

    def test_csrf_and_dns_rebinding_protection(self):
        for headers in ({"X-Review-Token": ""}, {"Origin": "http://evil.invalid"}, {"Host": "evil.invalid"}):
            status, _, _ = self.request("POST", "/api/review/" + self.alias, review(), headers=headers)
            self.assertEqual(status, 403)
        status, _, _ = self.request("GET", "/api/runs", headers={"X-Review-Token": ""})
        self.assertEqual(status, 403)
        self.assertFalse(self.store.get_run("SECRET-MODEL-run")["reviewed"])

    def test_no_artifact_file_route_or_get_reveal(self):
        for path in ("/api/reveal/" + self.alias, "/runs/SECRET-MODEL-run/result.json", "/../../result.json"):
            status, _, _ = self.request("GET", path)
            self.assertEqual(status, 404)

    def test_review_context_only_exposes_spec_and_before_after_sources(self):
        run_id = "SECRET-MODEL-run"
        self.store.write_artifact(run_id, "problem.md", "Public extension specification")
        self.store.write_artifact(run_id, "baseline/main.py", "print('before')")
        self.store.write_artifact(run_id, "source/main.py", "print('<script>after</script>')")
        (self.store.run_dir(run_id) / "source/leak.json").symlink_to(self.store.run_dir(run_id) / "result.json")
        path = "/api/context/" + self.alias
        status, body, _ = self.request("GET", path)
        self.assertEqual(status, 200)
        listing = json.loads(body)
        self.assertTrue(listing["has_spec"])
        self.assertEqual(listing["files"], [{"view": "before", "file": "main.py"}, {"view": "after", "file": "main.py"}])
        for query, expected in (("?view=spec", "Public extension specification"),
                                ("?view=before&file=main.py", "print('before')"),
                                ("?view=after&file=main.py", "print('<script>after</script>')")):
            status, body, _ = self.request("GET", path + query)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["text"], expected)
        for query in ("?view=after&file=../result.json", "?view=after&file=leak.json",
                      "?view=result&file=result.json", "?view=spec&file=metadata.json",
                      "?view=before&file=/etc/passwd"):
            status, body, _ = self.request("GET", path + query)
            self.assertEqual(status, 400)
            self.assertNotIn("SECRET-MODEL", body)

    def test_page_escapes_untrusted_diff_and_pauses_when_hidden(self):
        status, body, headers = self.request("GET", "/?token=" + self.server.review_token)
        self.assertEqual(status, 200)
        self.assertNotIn("SECRET-MODEL", body)
        self.assertNotIn("<script>alert(1)</script>", body)
        self.assertIn("$('diff').textContent=data.source_diff", body)
        self.assertNotIn("innerHTML", body)
        self.assertIn("visibilitychange", body)
        self.assertIn("!document.hidden", body)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Cache-Control"], "no-store")


if __name__ == "__main__":
    unittest.main()
