"""Source-manifest validation and isolation for single-language starter exports."""

import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from export_public import export_public
from starter_support import load_starter
from run import ROOT, ContractError


class StarterPackagingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.source = self.directory / "source"
        self.source.mkdir()
        (self.source / ".git").mkdir()
        for name in ("run.py", "query-null/PROBLEM.md", "query-null/cases.json"):
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, path)
        self.project = self.source / "starters" / "query-null" / "python"
        self.project.mkdir(parents=True)
        (self.project / "main.py").write_text("print('public starter')\n")
        (self.project / "run.sh").write_text('#!/bin/sh\nexec python3 "$(dirname "$0")/main.py"\n')
        (self.project / "run.sh").chmod(0o755)
        self.manifest = {"schema_version": 1, "task": "query-null", "language": "python",
                         "entrypoint": "run.sh", "build": None, "files": ["main.py", "run.sh"]}
        self.write_manifest()

    def write_manifest(self):
        (self.project / "starter.json").write_text(json.dumps(self.manifest))

    def test_export_preserves_only_selected_language_and_executable_modes(self):
        other = self.source / "starters" / "query-null" / "prism"
        other.mkdir()
        (other / "secret.pr").write_text("OTHER-LANGUAGE-CODE")
        (self.project / "unlisted-private.txt").write_text("UNLISTED-CONTENT")
        output = export_public("query-null", self.directory / "bundle", self.source, "python")
        files = {str(path.relative_to(output / 'starter')) for path in (output / 'starter').rglob('*') if path.is_file()}
        self.assertEqual(files, {"starter.json", "main.py", "run.sh"})
        self.assertTrue((output / "starter" / "run.sh").stat().st_mode & 0o111)
        manifest = json.loads((output / 'MANIFEST.json').read_text())
        self.assertEqual(manifest['language'], 'python')
        self.assertEqual(manifest['starter_sha256'], load_starter('query-null', 'python', self.source).digest())
        for path in output.rglob('*'):
            if path.is_file():
                self.assertNotIn(b'OTHER-LANGUAGE-CODE', path.read_bytes())
                self.assertNotIn(b'UNLISTED-CONTENT', path.read_bytes())

    def test_parent_path_and_build_outputs_are_rejected(self):
        for name in ('../prism/secret.pr', '/etc/passwd', '.build/runner', 'node_modules/code.js', 'heldout/cases.json'):
            with self.subTest(name=name):
                self.manifest['files'] = ['main.py', 'run.sh', name]
                self.write_manifest()
                with self.assertRaises(ContractError):
                    load_starter('query-null', 'python', self.source)

    def test_symlink_source_is_rejected(self):
        target = self.directory / 'private.py'
        target.write_text('PRIVATE')
        (self.project / 'linked.py').symlink_to(target)
        self.manifest['files'].append('linked.py')
        self.write_manifest()
        with self.assertRaisesRegex(ContractError, 'regular file'):
            load_starter('query-null', 'python', self.source)

    def test_entrypoint_must_be_bundled(self):
        self.manifest['entrypoint'] = 'not-in-export.sh'
        self.write_manifest()
        with self.assertRaisesRegex(ContractError, 'allowlist'):
            load_starter('query-null', 'python', self.source)

    def test_symlinked_language_directory_is_rejected(self):
        outside = self.directory / 'outside'
        self.project.rename(outside)
        self.project.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ContractError, 'must not be symlinks'):
            load_starter('query-null', 'python', self.source)

    def test_symlinked_manifest_is_rejected(self):
        outside = self.directory / 'outside.json'
        (self.project / 'starter.json').rename(outside)
        (self.project / 'starter.json').symlink_to(outside)
        with self.assertRaisesRegex(ContractError, 'must not be symlinks'):
            load_starter('query-null', 'python', self.source)

    def test_launcher_must_be_executable(self):
        (self.project / 'run.sh').chmod(0o644)
        with self.assertRaisesRegex(ContractError, 'must be executable'):
            load_starter('query-null', 'python', self.source)

    def test_fingerprint_covers_execution_mode(self):
        before = load_starter('query-null', 'python', self.source).digest()
        (self.project / 'main.py').chmod(0o755)
        after = load_starter('query-null', 'python', self.source).digest()
        self.assertNotEqual(before, after)


if __name__ == '__main__':
    unittest.main()
