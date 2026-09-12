#!/usr/bin/env python3
"""Verify or explicitly refresh the separate checkpoint-two manifest."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from run import ROOT, runner


def snapshot():
    paths=[p for p in ROOT.rglob('*') if p.is_file() and p.suffix in ('.py','.md','.json') and p.name!='MANIFEST.json' and '__pycache__' not in p.parts]
    paths.extend([ROOT.parent/'run.py',ROOT.parent/'experiments'/'sandbox.py'])
    return {'schema_version':1,'checkpoint':2,'files':{os.path.relpath(p,ROOT):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)},'corpora':{name:{'count':len(cases),'sha256':runner.corpus_digest(cases)} for name,base in [('public',ROOT),('heldout',ROOT/'evaluator'/'heldout')] for cases in [runner.load_cases(base)]}}


def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--write',action='store_true'); a=p.parse_args()
    target=ROOT/'evaluator'/'MANIFEST.json'; actual=snapshot()
    if a.write: runner.write_report(target,actual); print('Wrote checkpoint-two evaluator manifest.'); return 0
    difference=runner.first_difference(json.loads(target.read_text()),actual)
    if difference: print(difference,file=sys.stderr); return 1
    print('Checkpoint-two specs, corpora, authoring tools and shared runner match the manifest.'); return 0

if __name__=='__main__': raise SystemExit(main())
