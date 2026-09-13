"""Produce viewer JSON from copies of frozen Prism sources; never regrade or edit them."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from report import read,sha

MANIFEST='''[package]
name = "{task}"
version = "0.0.0"
authors = ["Evaluation review"]
maintainers = ["review@example.invalid"]
license = "MIT"
src = "."
[bin]
entry = "main.pr"
'''

def export(root,output,compiler):
    import sys
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    from experiments.control import tree_fingerprint
    target=output/'prism';target.mkdir(exist_ok=True)
    cache={};records={}
    version=subprocess.run([str(compiler),'--version'],capture_output=True,text=True,check=True).stdout.strip()
    for c in read(root/'plan.json')['runs']:
        if c['language']!='prism':continue
        d=root/'runs'/c['run_id'];out=target/c['run_id'];out.mkdir(exist_ok=True);receipt={}
        for label,folder in [('before','baseline'),('after','source')]:
            source=d/folder
            if not source.exists():receipt[label]={'available':False,'reason':'Final archive was not retained'};continue
            digest=tree_fingerprint(source);key=(digest,c['task'])
            dest=out/(label+'.json')
            if key in cache:
                previous,info=cache[key]
                if previous:shutil.copyfile(previous,dest)
                receipt[label]=dict(info);continue
            with tempfile.TemporaryDirectory(prefix='prism-review-source-') as temporary:
                copy=Path(temporary)/'source';shutil.copytree(source,copy)
                # Index all modules, not just main.pr. The adapter is confined to
                # the copy and recorded; original code and package files stay intact.
                manifest=copy/'prism.toml'
                adapted=not manifest.exists()
                if adapted:manifest.write_text(MANIFEST.format(task=c['task']))
                try:
                    p=subprocess.run([str(compiler),'index',str(copy),'--no-compiler-cache','--out',str(dest)],capture_output=True,text=True,timeout=180)
                    diagnostic=p.stderr.replace(str(copy),'<snapshot>')
                    (out/(label+'.log')).write_text(diagnostic)
                    info={'available':p.returncode==0 and dest.exists(),'source_sha256':digest,'exit_code':p.returncode,'synthetic_manifest':adapted}
                except subprocess.TimeoutExpired:
                    info={'available':False,'source_sha256':digest,'reason':'Index generation exceeded 180 seconds','synthetic_manifest':adapted}
                if info['available']:
                    index=read(dest)
                    # Every indexed source must match the frozen file byte for byte.
                    for m in index['modules']:
                        if m.get('source') is not None and (source/m['path']).read_text()!=m['source']:
                            raise ValueError('index source differs from frozen file')
                    info.update(index_sha256=sha(dest),modules=len(index['modules']),definitions=len(index['defs']),
                                parse_errors=[{'module':m['dotted'],'error':m['error']} for m in index['modules'] if m.get('error')],
                                definitions_without_hash=sum(not x.get('hash') for x in index['defs']),tests=index['envelope'].get('tests'))
                else:dest.unlink(missing_ok=True)
            if tree_fingerprint(source)!=digest:raise ValueError('original source changed')
            receipt[label]=info;cache[key]=(dest if info['available'] else None,dict(info))
        if all(receipt[x]['available'] for x in ['before','after']):
            p=subprocess.run([str(compiler),'index','--diff',str(out/'before.json'),str(out/'after.json'),'--out',str(out/'diff.json')],capture_output=True,text=True,timeout=60)
            receipt['diff']={'available':p.returncode==0,'exit_code':p.returncode}
            if p.returncode==0:receipt['diff']['sha256']=sha(out/'diff.json')
            else:(out/'diff.log').write_text(p.stderr)
        records[c['run_id']]=receipt
        print(c['run_id'],{k:v['available'] for k,v in receipt.items()},flush=True)
    value={'compiler_version':version,'compiler_sha256':sha(compiler),'purpose':'Review-only projection. Original runs used pinned Prism 0.18.0; indexes use this newer viewer-compatible compiler. Missing hashes and parse errors are retained, not repaired. No new model calls or grading.',
           'synthetic_manifest':MANIFEST,'runs':records}
    (target/'manifest.json').write_text(json.dumps(value,indent=2)+'\n')
    return value

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for a in ['results','output','compiler']:p.add_argument('--'+a,required=True,type=Path)
    a=p.parse_args();export(a.results,a.output,a.compiler)
