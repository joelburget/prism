#!/usr/bin/env python3
"""Measure incremental-view scaling; no uncalibrated pass/fail time threshold."""
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import statistics
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
try:
    from ..run import runner
except ImportError:
    from run import runner


def workload(size,updates=128,profile='point-updates'):
    def table(name,rows):
        return {'name':name,'columns':[{'name':'k','type':'int','nullable':False},{'name':'v','type':'int','nullable':True}],'rows':rows}
    database=[table('l',[[i,i] for i in range(size)]),table('r',[[i,i] for i in range(size)])]
    if profile=='unrelated-table': database.append(table('u',[[0,0]]))
    sql='SELECT COUNT(*) AS n, SUM(r.v) AS s FROM l LEFT JOIN r ON l.k = r.k'
    commands=[{'op':'create','view':'v','sql':sql,'optimize':True}]
    replies=[{'ok':True,'result':{'view':'v','revision':0}}]
    values=list(range(size)); total=sum(values)
    for i in range(updates):
        rid=i%size+1; value=(i*17)%1009
        target='u' if profile=='unrelated-table' else 'r'
        commands.append({'op':'apply','changes':[{'op':'update','table':target,'id':1 if target=='u' else rid,'row':[0 if target=='u' else rid-1,value]}]})
        if target=='r': total += value-values[rid-1]; values[rid-1]=value
        replies.append({'ok':True,'result':{'revision':i+1}})
        commands.append({'op':'read','view':'v'})
        replies.append({'ok':True,'result':{'revision':i+1,'columns':['n','s'],'rows':[[size,total]]}})
    return {'protocol_version':1,'task':'query-null','input':{'database':database,'commands':commands}}, {'ok':True,'result':{'results':replies}}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--command',required=True); p.add_argument('--cwd')
    p.add_argument('--sizes',nargs='+',type=int,default=[512,4096,8192])
    p.add_argument('--updates',type=int,default=128); p.add_argument('--repetitions',type=int,default=3)
    p.add_argument('--timeout',type=runner.positive_timeout,default=120)
    p.add_argument('--report',type=Path,required=True)
    a=p.parse_args()
    if len(a.sizes)<2 or sorted(set(a.sizes))!=a.sizes or any(not 1<=n<=9999 for n in a.sizes) or not 1<=a.updates<=999 or not 1<=a.repetitions<=20:
        p.error('need ascending unique sizes 1..9999, updates 1..999, repetitions 1..20')
    command=shlex.split(a.command)
    if not command: p.error('empty command')
    rows=[]
    for profile in ('point-updates','unrelated-table'):
        for size in a.sizes:
            request,expected=workload(size,a.updates,profile)
            digest=hashlib.sha256(json.dumps(request,sort_keys=True).encode()).hexdigest()
            samples=[]; failures=[]
            # First repetition is warmup, retained in the report but excluded from median.
            for repetition in range(a.repetitions+1):
                result=runner.invoke(command,request,a.timeout,a.cwd)
                failure=result.get('reason') if result['status']!='received' else runner.first_difference(expected,result['actual'])
                if failure: failures.append({'repetition':repetition,'reason':failure})
                samples.append(result['elapsed_seconds'])
            row={'profile':profile,'size':size,'updates':a.updates,'request_sha256':digest,'samples_seconds':samples,'median_seconds':statistics.median(samples[1:]),'correct':not failures,'failures':failures}
            rows.append(row); print(json.dumps(row),flush=True)
    growth={profile:next(r['median_seconds'] for r in reversed(rows) if r['profile']==profile)/next(r['median_seconds'] for r in rows if r['profile']==profile) for profile in ('point-updates','unrelated-table')}
    runner.write_report(a.report,{'schema_version':1,'command':command,'cwd':a.cwd,'profiles':rows,'largest_to_smallest_time_ratio':growth,'performance_acceptance':None,'note':'Diagnostic only. Freeze machine/toolchain-specific thresholds after calibration; correctness is checked independently. Includes process startup and initial view creation.'})
    return 0 if all(r['correct'] for r in rows) else 1

if __name__=='__main__': raise SystemExit(main())
