"""Evaluator-only executable model of leased mode, never exported to agents.

Single-step state transitions make expected traces auditable. Legacy semantics
are covered by copied checkpoint-one fixtures, not implemented by this model.
"""
from copy import deepcopy
import re

LIMIT = 2_147_483_647

class Fault(Exception):
    pass

def integer(x, lo=0, hi=LIMIT):
    return type(x) is int and lo <= x <= hi

def ident(x):
    return isinstance(x, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', x) is not None

def validate(data):
    if not isinstance(data, dict) or not {'steps','commands','workers'} <= data.keys() or data.keys() - {'steps','commands','workers','max_attempts','retry_delay','lease_duration'}:
        raise Fault('INVALID_INPUT')
    for key, default, hi in [('max_attempts',3,10),('retry_delay',2,1_000_000),('lease_duration',5,1_000_000)]:
        if not integer(data.get(key,default),1,hi): raise Fault('INVALID_INPUT')
    workers = data['workers']
    if not isinstance(workers,list) or not workers or any(not ident(w) for w in workers) or len(set(workers)) != len(workers): raise Fault('INVALID_INPUT')
    steps = data['steps']
    if not isinstance(steps,list) or not steps: raise Fault('INVALID_INPUT')
    for s in steps:
        if not isinstance(s,dict) or not {'id','needs','amount'} <= s.keys() or s.keys()-{'id','needs','amount','failures'}: raise Fault('INVALID_INPUT')
        if not ident(s['id']) or not isinstance(s['needs'],list) or any(not ident(n) for n in s['needs']) or len(set(s['needs'])) != len(s['needs']): raise Fault('INVALID_INPUT')
        if not integer(s['amount'],1,1_000_000) or not integer(s.get('failures',0),0,100): raise Fault('INVALID_INPUT')
    fields={'start':{'run'},'cancel':{'run'},'advance':{'by'},'observe':set(),'crash':{'worker'},'restart':{'worker'},'claim':{'worker'},'renew':{'worker','ticket'},'call':{'worker','ticket'},'deliver':{'ticket'}}
    commands=data['commands']
    if not isinstance(commands,list) or len(commands)>2000: raise Fault('INVALID_INPUT')
    for c in commands:
        if not isinstance(c,dict) or not isinstance(c.get('op'),str) or c['op'] not in fields or set(c) != fields[c['op']] | {'op'}: raise Fault('INVALID_INPUT')
        if any(not ident(c[k]) for k in ('worker','run') if k in c): raise Fault('INVALID_INPUT')
        if 'ticket' in c and not integer(c['ticket'],1): raise Fault('INVALID_INPUT')
        if 'by' in c and not integer(c['by']): raise Fault('INVALID_INPUT')
    ids=[s['id'] for s in steps]
    if len(set(ids)) != len(ids): raise Fault('DUPLICATE_STEP')
    if any(n not in ids for s in steps for n in s['needs']): raise Fault('UNKNOWN_DEPENDENCY')
    done=set()
    while len(done)<len(steps):
        ready={s['id'] for s in steps if s['id'] not in done and set(s['needs']) <= done}
        if not ready: raise Fault('DEPENDENCY_CYCLE')
        done |= ready

class Model:
    def __init__(self, data):
        validate(data)
        self.graph=data['steps']; self.workers={w:True for w in data['workers']}
        self.duration=data.get('lease_duration',5); self.delay=data.get('retry_delay',2); self.maximum=data.get('max_attempts',3)
        self.now=0; self.runs={}; self.tickets={}; self.calls=[]; self.effects={}; self.failures={}

    def snapshot(self, final=False):
        out={'now':self.now,'workers':[{'id':w,'up':up} for w,up in self.workers.items()], 'runs':list(self.runs.values())}
        if final: out.update(calls=self.calls,effects=list(self.effects.values()))
        return deepcopy(out)

    def add_time(self, amount):
        if self.now+amount>LIMIT: raise Fault('TIME_OVERFLOW')
        return self.now+amount

    def step_for(self,t):
        run=self.runs[t['run']]
        return run, next(s for s in run['steps'] if s['id']==t['step'])

    def live(self,t):
        _,s=self.step_for(t)
        return s['status']=='running' and s['lease']['ticket']==t['ticket'] and self.now<s['lease']['expires']

    def expire_running(self, run):
        for s in run['steps']:
            if s['status']=='running': s['lease']['expires']=self.now

    def finish(self,run):
        running=any(s['status']=='running' for s in run['steps'])
        if run['status']=='cancelling' and not running: run['status']='cancelled'
        elif run['status']=='failing' and not running: run['status']='failed'
        elif run['status']=='active' and all(s['status']=='succeeded' for s in run['steps']): run['status']='succeeded'

    def command(self,c):
        op=c['op']; w=c.get('worker'); ticket=c.get('ticket')
        if w is not None:
            if w not in self.workers: raise Fault('UNKNOWN_WORKER')
            if op=='restart':
                if self.workers[w]: raise Fault('WORKER_UP')
            elif not self.workers[w]: raise Fault('WORKER_DOWN')
        if ticket is not None:
            if ticket not in self.tickets: raise Fault('UNKNOWN_TICKET')
            t=self.tickets[ticket]
            if w is not None and t['worker']!=w: raise Fault('WRONG_WORKER')
        if op=='observe': return self.snapshot()
        if op=='advance': self.now=self.add_time(c['by']); return None
        if op in ('crash','restart'): self.workers[w]=op=='restart'; return None
        if op=='start':
            if c['run'] in self.runs: raise Fault('DUPLICATE_RUN')
            self.runs[c['run']]={'id':c['run'],'status':'active','cancel_requested':False,'steps':[{'id':s['id'],'status':'pending','attempts':0,'ready_at':self.now,'lease':None} for s in self.graph]}
            return None
        if op=='cancel':
            if c['run'] not in self.runs: raise Fault('UNKNOWN_RUN')
            run=self.runs[c['run']]
            if run['status']!='active': return None
            run['cancel_requested']=True; run['status']='cancelling'
            for s in run['steps']:
                if s['status']=='pending': s['status']='cancelled'
            self.expire_running(run); self.finish(run); return None
        if op=='claim':
            if any(s['lease'] and s['lease']['worker']==w and self.now<s['lease']['expires'] for r in self.runs.values() for s in r['steps']): raise Fault('WORKER_BUSY')
            all_steps=[(r,s) for r in self.runs.values() for s in r['steps']]
            candidates=[(r,s) for r,s in all_steps if s['status']=='running' and self.now>=s['lease']['expires']]
            if not candidates:
                for r,s in all_steps:
                    definition=next(g for g in self.graph if g['id']==s['id'])
                    succeeded={x['id'] for x in r['steps'] if x['status']=='succeeded'}
                    if r['status']=='active' and s['status']=='pending' and s['ready_at']<=self.now and set(definition['needs']) <= succeeded:
                        candidates.append((r,s))
            if not candidates: return {'ticket':None}
            expires=self.add_time(self.duration); run,s=candidates[0]
            number=len(self.tickets)+1
            if s['status']=='pending': s['attempts']+=1; s['status']='running'
            s['lease']={'worker':w,'ticket':number,'expires':expires}
            self.tickets[number]={'ticket':number,'worker':w,'run':run['id'],'step':s['id'],'attempt':s['attempts'],'kind':'execute' if run['status']=='active' else 'lookup','response':None,'delivered':False}
            return {'ticket':number}
        if op=='renew':
            if not self.live(t): return {'renewed':False}
            self.step_for(t)[1]['lease']['expires']=self.add_time(self.duration)
            return {'renewed':True}
        if op=='call':
            if not self.live(t): return {'outcome':'stale'}
            if t['response'] is not None: return deepcopy(t['response'])
            key=(t['run'],t['step']); definition=next(s for s in self.graph if s['id']==t['step'])
            if t['kind']=='lookup': outcome='found' if key in self.effects else 'missing'
            elif key in self.effects: outcome='replayed'
            elif self.failures.get(key,0)<definition.get('failures',0):
                self.failures[key]=self.failures.get(key,0)+1; outcome='transient'
            else:
                outcome='applied'; self.effects[key]={'key':list(key),'amount':definition['amount']}
            t['response']={'kind':t['kind'],'outcome':outcome}
            self.calls.append({'worker':w,'ticket':ticket,'kind':t['kind'],'key':list(key),'attempt':t['attempt'],'outcome':outcome})
            return deepcopy(t['response'])
        if op=='deliver':
            if t['response'] is None or t['delivered'] or not self.workers[t['worker']] or not self.live(t): return {'committed':False}
            run,s=self.step_for(t); outcome=t['response']['outcome']
            if t['kind']=='lookup': s['status']='succeeded' if outcome=='found' else ('cancelled' if run['status']=='cancelling' else 'blocked')
            elif outcome!='transient': s['status']='succeeded'
            elif s['attempts']<self.maximum: s['ready_at']=self.add_time(self.delay); s['status']='pending'
            else:
                s['status']='failed'; run['status']='failing'
                for other in run['steps']:
                    if other['status']=='pending': other['status']='blocked'
                self.expire_running(run)
            s['lease']=None; t['delivered']=True; self.finish(run)
            return {'committed':True}
        raise AssertionError(op)

def evaluate(data):
    try:
        model=Model(data)
        results=[model.command(c) for c in data['commands']]
        return {'ok':True,'result':{'results':results,'final':model.snapshot(final=True)}}
    except Fault as e:
        return {'ok':False,'error':{'code':str(e)}}
