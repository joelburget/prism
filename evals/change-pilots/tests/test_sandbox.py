"""Unit security checks plus opt-in, no-model Docker verification.

PRISM_EVAL_DOCKER_TESTS=1 python3 -m unittest discover -s evals/change-pilots/tests -p test_sandbox.py -v
"""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "experiments"))
from build_image import DEFAULT_IMAGE, PRISM_COMMIT, prepare_context
from export_public import export_public
from run import TASKS, first_difference, load_cases, parse_json, response_contract
from sandbox import (DockerEvaluation, DockerSandbox, InvalidSubmissionError, SandboxError,
                     SandboxFrozenError, SubmissionBuildError, _capture, _limits, _regular_tree,
                     extract_source_archive)
from starter_support import LANGUAGES, load_starter


class SandboxUnitTests(unittest.TestCase):
    def archive(self, entries):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            for name, kind, data in entries:
                info = tarfile.TarInfo(name)
                info.type = kind
                info.mode = 0o755 if name.endswith(".sh") else 0o644
                if kind == tarfile.REGTYPE:
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
                else:
                    info.linkname = data.decode()
                    archive.addfile(info)
        return buffer.getvalue()

    def extract(self, entries):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "snapshot.tar"
            archive.write_bytes(self.archive(entries))
            result = extract_source_archive(archive, root / "source")
            return {p.relative_to(result).as_posix(): (p.read_bytes(), p.stat().st_mode & 0o777)
                    for p in result.rglob("*") if p.is_file()}

    def test_snapshot_sources_and_build_exclusion(self):
        output = self.extract([("starter/run.sh", tarfile.REGTYPE, b"#!/bin/sh\n"),
                               ("starter/new.py", tarfile.REGTYPE, b"print(1)\n"),
                               ("starter/.build/compiled", tarfile.REGTYPE, b"\0ELF")])
        self.assertEqual(set(output), {"run.sh", "new.py"})
        self.assertEqual(output["run.sh"][1], 0o755)

    def test_snapshot_rejects_path_escape_links_special_and_binary(self):
        for entry in [("starter/../../outside.py", tarfile.REGTYPE, b"bad"),
                      ("/starter/outside.py", tarfile.REGTYPE, b"bad"),
                      ("starter/x.py", tarfile.SYMTYPE, b"/etc/passwd"),
                      ("starter/x.py", tarfile.LNKTYPE, b"starter/run.sh"),
                      ("starter/x.py", tarfile.FIFOTYPE, b""),
                      ("starter/x.py", tarfile.REGTYPE, b"\0binary"),
                      ("starter/x.exe", tarfile.REGTYPE, b"binary")]:
            with self.subTest(entry=entry), self.assertRaises(SandboxError):
                self.extract([("starter/run.sh", tarfile.REGTYPE, b"true"), entry])

    def test_snapshot_rejects_oversize_and_duplicates(self):
        entry = ("starter/run.sh", tarfile.REGTYPE, b"true")
        with self.assertRaises(SandboxError):
            self.extract([entry, entry])
        with patch("sandbox.SOURCE_LIMIT", 1), self.assertRaises(SandboxError):
            self.extract([entry])

    def test_upload_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            (path / "secret").symlink_to("/etc/passwd")
            with self.assertRaises(SandboxError):
                list(_regular_tree(path))

    def test_output_and_time_are_bounded(self):
        output = _capture([sys.executable, "-c", "print('x' * 1000000)"], output_limit=100)
        self.assertTrue(output["output_limited"])
        self.assertEqual(len(output["stdout"]), 100)
        timeout = _capture([sys.executable, "-c", "import time; time.sleep(30)"], timeout_seconds=0.1)
        self.assertTrue(timeout["timed_out"])
        self.assertLess(timeout["elapsed_seconds"], 2)

    def test_limits_have_no_mount_network_or_privilege(self):
        arguments = _limits()
        self.assertEqual(arguments[arguments.index("--network") + 1], "none")
        self.assertEqual(arguments[arguments.index("--cap-drop") + 1], "ALL")
        self.assertNotIn("--volume", arguments)
        self.assertNotIn("--mount", arguments)
        self.assertIn("no-new-privileges", arguments)
        self.assertIn("nofile=1024:1024", arguments)

    def test_frozen_sandbox_rejects_shell(self):
        sandbox = DockerSandbox("unused", Path("unused"))
        sandbox.frozen = True
        with self.assertRaises(SandboxFrozenError):
            sandbox.execute("echo shouldn't run")

    def test_image_context_has_only_pinned_compiler_sources(self):
        repository = ROOT.parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            context = Path(temporary) / "context"
            provenance = prepare_context(repository, context)
            paths = [p.relative_to(context).parts for p in context.rglob("*")]
            self.assertTrue(paths)
            self.assertTrue(all("evals" not in p and ".git" not in p for p in paths))
            self.assertEqual(provenance["prism_commit"], PRISM_COMMIT)
            self.assertIn('version = "0.22.0"', (context / "compiler/Cargo.toml").read_text())
            for path in ("docs/src/spec.md", "docs/src/compiler.md", "docs/src/tutorial.md",
                         "docs/src/tutorial/effects.md", "packages/lint/src/Lint.pr"):
                self.assertTrue((context / "compiler" / path).is_file(), path)


@unittest.skipUnless(os.environ.get("PRISM_EVAL_DOCKER_TESTS") == "1", "opt-in Docker tests")
class DockerIntegrationTests(unittest.TestCase):
    image = os.environ.get("PRISM_EVAL_IMAGE", DEFAULT_IMAGE)

    def test_isolation_freeze_and_case_reset(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = root / "bundle"
            (bundle / "starter").mkdir(parents=True)
            run = bundle / "starter/run.sh"
            run.write_text('#!/bin/sh\nif test -e /tmp/case-seen; then exit 9; fi\ntouch /tmp/case-seen\ncat\n')
            run.chmod(0o755)
            with DockerSandbox(self.image, bundle) as sandbox:
                result = sandbox.execute("test ! -e /var/run/docker.sock && test ! -e /src && test ! -e /work/evals && test $(id -u) = 1000 && test -f /opt/prism-docs/spec.md && test -f /opt/prism-lib/std/Json.pr")
                self.assertEqual(result["exit_code"], 0, result)
                config = json.loads(subprocess.check_output(["docker", "inspect", sandbox.container]))[0]
                self.assertEqual(config["HostConfig"]["NetworkMode"], "none")
                self.assertEqual(config["Mounts"], [])
                self.assertFalse(any("API_KEY=" in entry for entry in config["Config"]["Env"]))
                source = sandbox.snapshot(root / "source")
                with self.assertRaises(SandboxError):
                    sandbox.execute("echo forbidden")
            with DockerEvaluation(self.image, source) as evaluator:
                for _ in range(2):
                    result = evaluator.run_input(b'{"ok":true}\n')
                    self.assertEqual(result["exit_code"], 0, result)
                    self.assertEqual(result["stdout"], '{"ok":true}\n')

    def test_timeout_kills_container_and_blocks_more_agent_calls(self):
        with tempfile.TemporaryDirectory() as temporary:
            with DockerSandbox(self.image, Path(temporary)) as sandbox:
                result = sandbox.execute("sleep 30", timeout_seconds=0.1)
                self.assertTrue(result["timed_out"], result)
                self.assertTrue(sandbox.frozen)
                with self.assertRaises(SandboxError):
                    sandbox.execute("echo forbidden")

    def test_invalid_submission_and_build_failure_are_distinct(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = root / "bundle"
            (bundle / "starter").mkdir(parents=True)
            for name, text in (("run.sh", "#!/bin/sh\ncat\n"),
                               ("build.sh", "#!/bin/sh\necho expected-compile-failure >&2\nexit 17\n")):
                path = bundle / "starter" / name
                path.write_text(text)
                path.chmod(0o755)
            with DockerSandbox(self.image, bundle) as sandbox:
                result = sandbox.execute("touch starter/new.py && mkdir starter/.build && echo generated > starter/.build/binary")
                self.assertEqual(result["exit_code"], 0, result)
                source = sandbox.snapshot(root / "source")
                self.assertTrue((source / "new.py").is_file())
                self.assertFalse((source / ".build").exists())
            with self.assertRaises(SubmissionBuildError) as raised:
                with DockerEvaluation(self.image, source, build="build.sh"):
                    self.fail("failed build cannot start case evaluation")
            self.assertEqual(raised.exception.build_result["exit_code"], 17)
            with DockerSandbox(self.image, bundle) as sandbox:
                self.assertEqual(sandbox.execute("ln -s /etc/passwd starter/stolen.py")["exit_code"], 0)
                with self.assertRaises(InvalidSubmissionError):
                    sandbox.snapshot(root / "invalid-source")

    def test_all_nine_starter_public_baselines(self):
        for task in TASKS:
            cases = [case for case in load_cases(tasks=[task]) if case.phase == "baseline"]
            for language in LANGUAGES:
                with self.subTest(task=task, language=language), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    bundle = export_public(task, root / "bundle", language=language)
                    starter = load_starter(task, language)
                    with DockerSandbox(self.image, bundle) as sandbox:
                        source = sandbox.snapshot(root / "source")
                    with DockerEvaluation(self.image, source, build=starter.build) as evaluator:
                        for case in cases:
                            result = evaluator.run_input(json.dumps(case.request).encode() + b"\n")
                            self.assertEqual(result["exit_code"], 0, f"{case.name}: {result}")
                            self.assertFalse(result["timed_out"] or result["output_limited"], result)
                            actual = parse_json(result["stdout"])
                            response_contract(actual)
                            self.assertIsNone(first_difference(case.expect, actual), case.name)


if __name__ == "__main__":
    unittest.main()
