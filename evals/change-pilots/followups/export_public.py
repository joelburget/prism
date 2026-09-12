#!/usr/bin/env python3
"""Allowlisted checkpoint-two export, optionally carrying a frozen predecessor."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile

from run import ROOT, runner


def digest(data): return hashlib.sha256(data).hexdigest()
def encoded(data): return (json.dumps(data,sort_keys=True,indent=2,allow_nan=False)+'\n').encode()

def source_files(root):
    if root.is_symlink() or not root.is_dir(): raise ValueError('source must be a regular directory')
    files={}; executable=set(); total=0
    for base,dirs,names in os.walk(root,followlinks=False):
        for name in dirs+names:
            path=Path(base)/name; mode=path.lstat().st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)): raise ValueError('symlinks/special source files are forbidden')
            relative=path.relative_to(root).as_posix()
            if '.git' in path.relative_to(root).parts: raise ValueError('Git history is forbidden in source')
            if stat.S_ISREG(mode):
                total+=path.stat().st_size
                if len(files)>=1000 or total>16*1024*1024: raise ValueError('source exceeds export bounds')
                files[relative]=path.read_bytes()
                if mode & 0o111: executable.add(relative)
    if 'run.sh' not in executable: raise ValueError('source needs executable run.sh')
    fingerprint=digest(encoded({name:{'sha256':digest(data),'executable':name in executable} for name,data in sorted(files.items())}))
    return files,executable,fingerprint


def export(task,destination,predecessor=None,cohort='chain'):
    if task not in runner.TASKS: raise ValueError('unknown task')
    destination=Path(destination).absolute()
    if destination.exists() or destination.is_symlink(): raise ValueError('destination must be new')
    repository=next((p for p in (ROOT,*ROOT.parents) if (p/'.git').exists()),ROOT)
    if destination.resolve()==repository.resolve() or repository.resolve() in destination.resolve().parents: raise ValueError('export outside the evaluator repository')
    runner.load_cases(ROOT,[task])
    files={'run.py':(ROOT/'run.py').read_bytes(),'public_runner.py':(ROOT.parent/'run.py').read_bytes(),f'{task}/PROBLEM.md':(ROOT/task/'PROBLEM.md').read_bytes(),f'{task}/PREVIOUS.md':(ROOT/task/'PREVIOUS.md').read_bytes(),f'{task}/cases.json':(ROOT/task/'cases.json').read_bytes()}
    executable=set(); provenance=None
    manifest={'schema_version':1,'checkpoint':2,'task':task}
    if predecessor:
        predecessor=Path(predecessor)
        metadata=json.loads((predecessor/'metadata.json').read_text())
        result=json.loads((predecessor/'result.json').read_text())
        if metadata['task']!=task or metadata['language'] not in ('prism','python','typescript'): raise ValueError('predecessor task/language mismatch')
        if result['status']!='completed': raise ValueError('predecessor must be a finished non-infrastructure run')
        sources,modes,fingerprint=source_files(predecessor/'source')
        if result.get('source_sha256')!=fingerprint: raise ValueError('predecessor source changed since grading')
        for corpus,base in [('public',ROOT.parent),('heldout',ROOT.parent/'heldout')]:
            old=runner.load_cases(base,[task]); score=result['scores'][corpus]
            if score['corpus_sha256']!=runner.corpus_digest(old): raise ValueError('predecessor graded against a different checkpoint-one corpus')
            if {c['id'] for c in score['cases']}!={c.id for c in old}: raise ValueError('predecessor missing case grades')
        if cohort=='controlled' and result.get('success') is not True: raise ValueError('controlled baseline must pass checkpoint one')
        files.update({'starter/'+name:data for name,data in sources.items()})
        executable={'starter/'+name for name in modes}
        manifest.update(language=metadata['language'],starter_sha256=fingerprint)
        provenance={'schema_version':1,'checkpoint':2,'cohort':cohort,'task':task,'language':metadata['language'],'parent_run_id':metadata['run_id'],'parent_source_sha256':fingerprint,'parent_passed':result.get('success'),'parent_metadata_sha256':digest((predecessor/'metadata.json').read_bytes()),'parent_result_sha256':digest((predecessor/'result.json').read_bytes())}
    files['README.md']=f'''# Checkpoint 2: {task}

Read `{task}/PREVIOUS.md` and `{task}/PROBLEM.md`. All previous behavior must
continue to work. Only this checkpoint's public materials and, when supplied,
the assigned language's frozen preceding source are included. Use a fresh session.

Build Prism with `./starter/build.sh`; launch all languages with `./starter/run.sh`.
Run public tests with:

```
python3 run.py validate --task {task}
python3 run.py run --task {task} --command './starter/run.sh'
```

`baseline` now includes the entire earlier public suite. `extension` covers the
new change. No evaluator files or other languages are supplied. Public test
iteration and agent-written tests are allowed. A tests-only bundle needs its
assigned predecessor sources before implementation work begins.

The shared process runner requires Python 3.10+, works across implementation
languages, and is not a security sandbox. The surrounding evaluator isolates
both agent and submitted process. Budgets are unchanged from checkpoint one.
'''.encode()
    manifest['files']={name:digest(data) for name,data in sorted(files.items())}
    files['MANIFEST.json']=encoded(manifest)
    destination.parent.mkdir(parents=True,exist_ok=True)
    temporary=Path(tempfile.mkdtemp(prefix='.followup-export-',dir=destination.parent))
    try:
        for name,data in files.items():
            path=temporary/name; path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes(data)
            path.chmod(0o755 if name in executable else 0o644)
        # mkdir first means we never replace a preexisting directory, even an empty one.
        destination.mkdir()
        for path in temporary.iterdir(): shutil.move(str(path),destination/path.name)
    finally: shutil.rmtree(temporary)
    if provenance: provenance['bundle_manifest_sha256']=digest((destination/'MANIFEST.json').read_bytes())
    return provenance


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--task',choices=runner.TASKS,required=True); p.add_argument('--output',type=Path,required=True)
    p.add_argument('--predecessor-run',type=Path); p.add_argument('--cohort',choices=['chain','controlled'],default='chain')
    p.add_argument('--receipt',type=Path,help='evaluator-only lineage receipt, outside the public bundle')
    a=p.parse_args()
    if bool(a.predecessor_run)!=bool(a.receipt): p.error('--predecessor-run and --receipt must be supplied together')
    if a.cohort=='controlled' and not a.predecessor_run: p.error('controlled cohort requires a qualified predecessor')
    if a.receipt:
        resolved=a.receipt.resolve(); bundle=a.output.resolve()
        if resolved==bundle or bundle in resolved.parents: p.error('lineage receipt must stay outside the public bundle')
        frozen_source=(a.predecessor_run/'source').resolve()
        if resolved==frozen_source or frozen_source in resolved.parents: p.error('lineage receipt must stay outside the frozen predecessor source')
        if a.receipt.exists() or a.receipt.is_symlink(): p.error('receipt must be new')
    try:
        receipt=export(a.task,a.output,a.predecessor_run,a.cohort)
        if receipt:
            a.receipt.parent.mkdir(parents=True,exist_ok=True)
            with a.receipt.open('x') as f: json.dump(receipt,f,indent=2); f.write('\n')
    except (OSError,ValueError,KeyError) as e: p.exit(2,f'Export failed: {e}\n')
    print(f'Exported checkpoint-two public bundle to {a.output}')

if __name__=='__main__': main()
