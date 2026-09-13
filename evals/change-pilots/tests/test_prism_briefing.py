"""Check frozen tutorial content and opt-in native execution of its examples."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from experiments.context.update_tutorial import CHAPTERS, MARKER, render
from experiments.context.verify_briefing import BRIEFING, verify
from experiments.native_task_setup import DEFAULT_IMAGE


class PrismBriefingTests(unittest.TestCase):
    def test_full_tutorial_is_frozen_from_pinned_compiler_source(self):
        tutorial, sources = render(ROOT.parents[1])
        manifest = json.loads(BRIEFING.with_name('TUTORIAL_SOURCES.json').read_text())
        self.assertEqual(len(CHAPTERS), 9)
        self.assertEqual(sources, manifest['sources_sha256'])
        self.assertEqual(BRIEFING.read_text().split(MARKER)[1].lstrip(), tutorial)
        self.assertNotIn('{{#tab', tutorial)
        self.assertNotIn('\n# type Reading', tutorial)

    @unittest.skipUnless(os.environ.get('PRISM_EVAL_DOCKER_TESTS') == '1' or os.environ.get('PRISM_BRIEFING_COMPILER'), 'opt-in Prism toolchain')
    def test_examples_compile_and_match_documented_output(self):
        compiler = os.environ.get('PRISM_BRIEFING_COMPILER')
        if compiler:
            receipt = verify(compiler)
        else:
            # Bind only the public briefing and verifier, never the repository.
            with tempfile.TemporaryDirectory(prefix='prism-context-docker-') as temporary:
                for path in (BRIEFING, BRIEFING.with_name('verify_briefing.py')):
                    shutil.copyfile(path, Path(temporary) / path.name)
                result = subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--memory', '2g',
                    '--cpus', '2', '--pids-limit', '128', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                    '--mount', f'type=bind,source={temporary},target=/work,readonly',
                    os.environ.get('PRISM_EVAL_NATIVE_TASK_IMAGE', DEFAULT_IMAGE),
                    'python3', '/work/verify_briefing.py'], capture_output=True, text=True, timeout=600)
                self.assertEqual(result.returncode, 0, result.stderr)
                receipt = json.loads(result.stdout)
        self.assertEqual(receipt['compiler_version'], 'prism 0.22.0')
        self.assertGreaterEqual(sum(e['status'] == 'executed' for e in receipt['examples']), 25)
        self.assertGreaterEqual(sum(e['status'] == 'expected_compile_failure' for e in receipt['examples']), 5)
        # The only excluded fragments are the upstream multi-file import and typed hole.
        self.assertEqual(sum(e['status'] == 'excluded' for e in receipt['examples']), 2)


if __name__ == '__main__':
    unittest.main()
