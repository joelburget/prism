"""Run briefing examples with the pinned image or a supplied Prism 0.18.0 binary."""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

BRIEFING=Path(__file__).resolve().parents[1]/'experiments/context/prism-0.18.md'
TASK_IMAGE='sha256:4f6df9a771a25185f6d354eac92bddbad1b2addb27fda5014d3cb0f9cfabee39'

class PrismBriefingExamples(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('PRISM_EVAL_DOCKER_TESTS')=='1' or os.environ.get('PRISM_BRIEFING_COMPILER'),'opt-in Prism 0.18.0 toolchain')
    def test_complete_examples_compile_and_match_documented_output(self):
        blocks=re.findall(r'```prism\n(.*?)```',BRIEFING.read_text(),re.S)
        expected=['5\n2\n','4\n6\n'];self.assertEqual(len(blocks),len(expected))
        compiler=os.environ.get('PRISM_BRIEFING_COMPILER')
        if compiler:
            version=subprocess.run([compiler,'--version'],capture_output=True,text=True,check=True).stdout.strip()
            self.assertEqual(version,'prism 0.18.0')
        with tempfile.TemporaryDirectory(prefix='prism-briefing-check-') as temporary:
            for number,(code,answer) in enumerate(zip(blocks,expected),1):
                with self.subTest(example=number):
                    path=Path(temporary)/'example.pr';path.write_text(code);path.chmod(0o644)
                    if compiler:
                        executable=Path(temporary)/'briefing-example'
                        compiled=subprocess.run([compiler,str(path),'-o',str(executable)],capture_output=True,text=True,timeout=90)
                        self.assertEqual(compiled.returncode,0,compiled.stderr)
                        result=subprocess.run([str(executable)],capture_output=True,text=True,timeout=10)
                    else:
                        result=subprocess.run(['docker','run','--rm','--network','none','--memory','2g',
                        '--cpus','2','--pids-limit','128','--cap-drop','ALL','--security-opt','no-new-privileges',
                        '--mount',f'type=bind,source={temporary},target=/work,readonly',TASK_IMAGE,
                        'sh','-c','prism /work/example.pr -o /tmp/briefing-example && /tmp/briefing-example'],
                        capture_output=True,text=True,timeout=90)
                    self.assertEqual(result.returncode,0,result.stderr)
                    self.assertEqual(result.stdout,answer)

if __name__=='__main__':unittest.main()
