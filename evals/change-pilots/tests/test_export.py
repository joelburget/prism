"""Check that agent exports are self-contained and contain only public allowlisted files."""

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from export_public import export_public
from run import ROOT, TASKS


class ExportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def test_each_export_is_runnable_and_has_only_fingerprinted_public_files(self):
        for task in TASKS:
            with self.subTest(task=task):
                output = export_public(task, self.directory / task)
                files = {str(path.relative_to(output)) for path in output.rglob('*') if path.is_file()}
                expected = {'run.py', 'README.md', 'MANIFEST.json', f'{task}/PROBLEM.md', f'{task}/cases.json'}
                self.assertEqual(files, expected)
                manifest = json.loads((output / 'MANIFEST.json').read_text())
                self.assertEqual(set(manifest['files']), expected - {'MANIFEST.json'})
                for name, digest in manifest['files'].items():
                    self.assertEqual(hashlib.sha256((output / name).read_bytes()).hexdigest(), digest)
                # Validate without any sibling task, source checkout, or private corpus.
                process = subprocess.run([sys.executable, 'run.py', 'validate', '--task', task],
                                         cwd=output, capture_output=True, text=True, timeout=5)
                self.assertEqual(process.returncode, 0, process.stderr)
                self.assertIn('Validated', process.stdout)

    def test_unknown_private_files_and_git_metadata_are_never_copied(self):
        source = self.directory / 'source'
        source.mkdir()
        for name in ('run.py', 'query-null/PROBLEM.md', 'query-null/cases.json'):
            target = source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)
        for name in ('.git/config', 'heldout/query-null/cases.json', 'reports/private.json', 'future-secret.txt'):
            target = source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('PRIVATE-CONTENT-SENTINEL')
        output = export_public('query-null', self.directory / 'bundle', source)
        for file in output.rglob('*'):
            if file.is_file():
                self.assertNotIn(b'PRIVATE-CONTENT-SENTINEL', file.read_bytes())
        self.assertFalse((output / '.git').exists())

    def test_existing_destination_is_not_merged_or_overwritten(self):
        output = self.directory / 'existing'
        output.mkdir()
        (output / 'keep').write_text('untouched')
        with self.assertRaisesRegex(ValueError, 'must not already exist'):
            export_public('query-null', output)
        self.assertEqual((output / 'keep').read_text(), 'untouched')

    def test_export_inside_source_repository_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'outside the evaluator repository'):
            export_public('query-null', ROOT / 'should-never-be-created')
        self.assertFalse((ROOT / 'should-never-be-created').exists())


if __name__ == '__main__':
    unittest.main()
