"""Create review commits from frozen snapshots without checking out a branch.

The initial commit seeds every chain with its actual baseline. Each subsequent
stage commit changes only that chain's source/specification and receipt. Missing
archives retain the preceding source and explicitly record that no final source
exists; they never invent an implementation. Nothing here executes a submission.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

from report import read, sha

class Git:
    def __init__(self, repo, index):
        self.repo=repo
        self.env={**os.environ,'GIT_INDEX_FILE':str(index)}
        self.objects={}
    def run(self,*args,data=None):
        return subprocess.run(['git','-C',str(self.repo),*args],input=data,capture_output=True,check=True,env=self.env).stdout.decode().strip()
    def blob(self,data):
        digest=hashlib.sha256(data).hexdigest()
        if digest not in self.objects:self.objects[digest]=self.run('hash-object','-w','--stdin',data=data)
        return self.objects[digest]
    def update(self,files,delete=()):
        lines=[f'0 {"0"*40}\t{name}\n' for name in sorted(delete)]
        for name,(mode,data) in sorted(files.items()):
            if any(c in name for c in '\n\t\0'):raise ValueError('unsupported Git path')
            lines.append(f'{mode} {self.blob(data)}\t{name}\n')
        self.run('update-index','--index-info',data=''.join(lines).encode())
    def commit(self,parent,message):
        tree=self.run('write-tree')
        return self.run('commit-tree',tree,'-p',parent,data=(message+'\n').encode())

def files_at(directory,prefix):
    result={}
    if directory.is_symlink():raise ValueError('symlink source')
    for p in sorted(directory.rglob('*')):
        if p.is_symlink():raise ValueError('symlink source')
        if p.is_file():
            result[prefix+'/'+p.relative_to(directory).as_posix()]=('100755' if p.stat().st_mode & 0o111 else '100644',p.read_bytes())
    return result

def js(value):return (json.dumps(value,indent=2)+'\n').encode()

def export(root,repo,output,branch,base='HEAD',github='https://github.com/joelburget/prism'):
    plan=read(root/'plan.json'); report=read(output/'runs.json'); rows={r['run_id']:r for r in report['runs']}
    manifest=output/'git-reviews.json'
    if manifest.exists():raise ValueError('review export already exists')
    if subprocess.run(['git','-C',str(repo),'show-ref','--verify','--quiet','refs/heads/'+branch]).returncode==0:
        raise ValueError('review branch already exists')
    subprocess.run(['git','check-ref-format','refs/heads/'+branch],check=True)
    import sys
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    from experiments.control import tree_fingerprint
    from starter_support import load_starter
    prefix='eval-reviews/2026-09-12-chained'
    paths={c['chain_id']:f"{prefix}/{c['task']}/{c['language']}/{c['model']['key']}/r{c['repetition']}" for c in plan['runs']}
    # Fail before any ref is published if frozen parentage or source has drifted.
    for c in plan['runs']:
        d=root/'runs'/c['run_id'];r=rows[c['run_id']]
        if sha(d/'result.json')!=r['result_sha256']:raise ValueError('result changed')
        if r['source_available'] and tree_fingerprint(d/'source')!=r['source_sha256']:raise ValueError('source changed')
        if c['checkpoint']==2:
            parent_source=root/'runs'/c['parent_run_id']/'source'
            # Baseline review artifacts were stored read-only, losing their mode.
            # Their bytes must match; the actual predecessor supplies Git modes.
            a={k:v[1] for k,v in files_at(d/'baseline','source').items()}
            b={k:v[1] for k,v in files_at(parent_source,'source').items()}
            if a!=b:raise ValueError('second checkpoint baseline differs from predecessor')
            if (d/'lineage.json').exists() and read(d/'lineage.json')['parent_source_sha256']!=tree_fingerprint(parent_source):
                raise ValueError('parent provenance differs from frozen source')
    with tempfile.TemporaryDirectory(prefix='prism-review-index-') as temp:
        git=Git(repo,Path(temp)/'index');parent=git.run('rev-parse',base);base_sha=parent
        git.run('read-tree',parent)
        if git.run('ls-tree','--name-only',parent,'--',prefix):raise ValueError('review prefix already present at base')
        current={}; seeded={}
        for c in plan['runs']:
            if c['checkpoint']!=1:continue
            d=root/'runs'/c['run_id'];path=paths[c['chain_id']]
            files=files_at(d/'baseline',path+'/starter')
            starter=load_starter(c['task'],c['language'])
            if starter.digest()!=c['starter_sha256']:raise ValueError('starter mode provenance changed')
            originals={path+'/starter/'+name:source for name,source in starter.source_files()}
            if set(originals)!=set(files):raise ValueError('starter files differ from baseline')
            for name,(mode,data) in list(files.items()):
                if originals[name].read_bytes()!=data:raise ValueError('starter bytes differ from baseline')
                files[name]=('100755' if originals[name].stat().st_mode & 0o111 else '100644',data)
            current[c['chain_id']]=set(files)
            files[path+'/PROBLEM.md']=('100644',(d/'problem.md').read_bytes())
            seeded.update(files)
        seeded[prefix+'/README.md']=('100644',b'# Chained evaluation review snapshots\n\nEach chain starts at its archived baseline. Each stage has its own commit. These are frozen model submissions, not reference solutions. Some fail or cannot be built. The two invalid archives have an explicit receipt and no fabricated final source.\n\nOpen the final report/index.html locally for filterable statistics, or report/SUMMARY.md on GitHub. Model identity is visible. Generated Prism indexes are review-only projections from a separately recorded compiler version; original evaluation grades are unchanged.\n')
        git.update(seeded);parent=git.commit(parent,'Seed all 48 evaluation chains with their archived baselines');baseline=parent
        commits={}
        for c in plan['runs']:
            r=rows[c['run_id']];d=root/'runs'/c['run_id'];path=paths[c['chain_id']]
            files={};delete=set()
            if r['source_available']:
                files=files_at(d/'source',path+'/starter');delete=current[c['chain_id']]-files.keys();current[c['chain_id']]=set(files)
            files[path+'/PROBLEM.md']=('100644',(d/'problem.md').read_bytes())
            if (d/'previous-problem.md').exists():files[path+'/PREVIOUS.md']=('100644',(d/'previous-problem.md').read_bytes())
            receipt={k:r[k] for k in ['run_id','checkpoint','model','model_id','task','language','source_available','source_sha256','passed','seconds','tool_calls','flags','result_sha256']}
            receipt['archive_note']='Exact frozen source' if r['source_available'] else 'Final archive unavailable. starter/ is the preceding baseline, NOT this stage’s submission.'
            files[path+'/stage.json']=('100644',js(receipt));git.update(files,delete)
            parent=git.commit(parent,f"{c['model']['key']}: {c['task']} in {c['language']}, checkpoint {c['checkpoint']}\n\nFrozen run {c['run_id']}. No model rerun.\n"+receipt['archive_note'])
            commits[c['run_id']]={'commit':parent,'path':path,'source_available':r['source_available'],'url':github+'/commit/'+parent}
        git.run('update-ref','refs/heads/'+branch,parent,'0'*40)
    value={'branch':branch,'base':base_sha,'baseline_commit':baseline,'stage_tip':parent,'prefix':prefix,'github':github,'commits':commits}
    manifest.write_text(json.dumps(value,indent=2)+'\n')
    return value

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for a in ['results','repo','output']:p.add_argument('--'+a,required=True,type=Path)
    p.add_argument('--branch',required=True);p.add_argument('--base',default='HEAD');a=p.parse_args()
    x=export(a.results,a.repo,a.output,a.branch,a.base);print(json.dumps({'branch':x['branch'],'stage_commits':len(x['commits']),'tip':x['stage_tip']}))
