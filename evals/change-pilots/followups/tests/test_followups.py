import importlib.util
import json
from pathlib import Path
import sys
import unittest
from copy import deepcopy

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'evaluator'))
import workflow_model as wm
import query_model as qm
import generate as gen
from scaling import workload


def cases(private=False,task=None):
    base=ROOT/'evaluator'/'heldout' if private else ROOT
    tasks=[task] if task else ['query-null','workflow-recovery']
    return [c for t in tasks for c in json.loads((base/t/'cases.json').read_text())['cases']]

def extension(private,task): return [c for c in cases(private,task) if c['phase']=='extension']

class CorpusTests(unittest.TestCase):
    def test_all_previous_cases_preserved(self):
        for private in (False,True):
            for task in ('query-null','workflow-recovery'):
                old=ROOT.parent/('heldout' if private else '')/task/'cases.json'
                previous=json.loads(old.read_text())['cases']
                actual=[c for c in cases(private,task) if c['phase']=='baseline']
                self.assertEqual(actual,[{**c,'id':'cp1-'+c['id'],'phase':'baseline'} for c in previous])
                self.assertEqual((ROOT/task/'PREVIOUS.md').read_bytes(),(ROOT.parent/task/'PROBLEM.md').read_bytes())

    def test_reproducible_generated_fixtures(self):
        for private in (False,True):
            for task in ('query-null','workflow-recovery'):
                self.assertEqual(extension(private,task),gen.DATA['heldout' if private else 'public'][task])

    def test_private_inputs_and_ids_disjoint(self):
        for task in ('query-null','workflow-recovery'):
            public=cases(False,task); private=cases(True,task)
            self.assertFalse({c['id'] for c in public}&{c['id'] for c in private})
            self.assertFalse({json.dumps(c['input'],sort_keys=True) for c in public}&{json.dumps(c['input'],sort_keys=True) for c in private})

    def test_paired_query_modes(self):
        for private in (False,True):
            by_id={c['id']:c for c in extension(private,'query-null')}
            for name,c in by_id.items():
                if name.endswith('-plain'):
                    other=by_id[name[:-6]+'-optimized']
                    self.assertEqual(c['expect'],other['expect'])

    def test_generated_workflows_invariants(self):
        for c in extension(True,'workflow-recovery'):
            if 'generated-' not in c['id']: continue
            out=c['expect']['result']; final=out['final']
            keys=[tuple(e['key']) for e in final['effects']]
            self.assertEqual(len(keys),len(set(keys)))
            applied=[tuple(call['key']) for call in final['calls'] if call['outcome']=='applied']
            self.assertEqual(keys,applied)
            self.assertTrue(all(r['status'] in ('succeeded','failed','cancelled') for r in final['runs']))
            self.assertTrue(all(s['lease'] is None for r in final['runs'] for s in r['steps']))
            tickets=[call['ticket'] for call in final['calls']]
            self.assertEqual(len(tickets),len(set(tickets)), 'one actual call per ticket')
            for call in final['calls']:
                if call['outcome'] in ('found','replayed'): self.assertIn(tuple(call['key']),keys)

class WorkflowAnchors(unittest.TestCase):
    def fixture(self,name,private=False):
        return next(c for c in extension(private,'workflow-recovery') if c['id']=='cp2-'+name)['expect']['result']

    def test_expired_success_hand_computed(self):
        result=self.fixture('expired-success')
        self.assertEqual(result['results'][:8],[None,{'ticket':1},{'kind':'execute','outcome':'applied'},None,{'ticket':2},{'committed':False},{'kind':'execute','outcome':'replayed'},{'committed':True}])
        final=result['final']
        self.assertEqual(final['effects'],[{'key':['r','s'],'amount':25}])
        self.assertEqual(final['runs'],[{'id':'r','status':'succeeded','cancel_requested':False,'steps':[{'id':'s','status':'succeeded','attempts':1,'ready_at':0,'lease':None}]}])
        self.assertEqual([c['attempt'] for c in final['calls']],[1,1])

    def test_cancel_mixed_lookup_hand_computed(self):
        out=self.fixture('cancel-two-running',True)
        self.assertEqual([c['outcome'] for c in out['final']['calls']],['applied','found','missing'])
        self.assertEqual([s['status'] for s in out['final']['runs'][0]['steps']],['cancelled','succeeded','cancelled'])
        self.assertEqual(out['final']['runs'][0]['status'],'cancelled')
        snapshots=[v for v in out['results'] if isinstance(v,dict) and 'runs' in v]
        self.assertEqual([s['status'] for s in snapshots[0]['runs'][0]['steps']],['running','running','cancelled'])
        self.assertEqual(snapshots[1]['runs'][0]['status'],'cancelling')

    def test_retry_attempts_and_delivery_clock(self):
        out=self.fixture('retry-from-delivery')
        self.assertEqual(out['results'][5]['runs'][0]['steps'][0]['ready_at'],5)
        self.assertEqual([c['attempt'] for c in out['final']['calls']],[1,2])
        out=self.fixture('uncommitted-transients',True)
        self.assertEqual([c['attempt'] for c in out['final']['calls']],[1,1,1])
        self.assertEqual(out['final']['runs'][0]['status'],'succeeded')

    def test_failure_drain_hand_computed(self):
        out=self.fixture('failure-drain-missing',True)
        run=out['final']['runs'][0]
        self.assertEqual((run['status'],run['cancel_requested']),('failed',False))
        self.assertEqual([s['status'] for s in run['steps']],['failed','blocked'])
        self.assertEqual(out['final']['effects'],[])
        self.assertEqual([c['kind'] for c in out['final']['calls']],['execute','lookup'])

    def test_error_anchors(self):
        expected={'renew-overflow':'TIME_OVERFLOW','claim-overflow':'TIME_OVERFLOW','retry-overflow':'TIME_OVERFLOW','command-prevalidation':'INVALID_INPUT','down-error-precedence':'WORKER_DOWN','wrong-owner':'WRONG_WORKER','unknown-ticket':'UNKNOWN_TICKET'}
        for c in extension(True,'workflow-recovery'):
            name=c['id'].removeprefix('cp2-')
            if name in expected: self.assertEqual(c['expect'],{'ok':False,'error':{'code':expected[name]}})

class QueryAnchors(unittest.TestCase):
    def fixture(self,name,private=False):
        return next(c for c in extension(private,'query-null') if c['id']=='cp2-'+name+'-plain')

    def reads(self,c):
        return [r['result'] for cmd,r in zip(c['input']['commands'],c['expect']['result']['results']) if cmd['op']=='read' and r['ok']]

    def test_outer_join_hand_computed(self):
        rows=[r['rows'] for r in self.reads(self.fixture('last-and-first-match'))]
        self.assertEqual(rows,[[[1,10,7],[1,10,8],[2,20,None],[None,30,None]],[[1,10,8],[2,20,None],[None,30,None]],[[1,10,None],[2,20,None],[None,30,None]],[[1,10,11],[2,20,None],[None,30,None]]])

    def test_duplicate_and_empty_aggregate_anchors(self):
        self.assertEqual([r['rows'] for r in self.reads(self.fixture('distinct-multiplicity'))],[[[4],[None]],[[4],[None]],[[None]],[[None]]])
        self.assertEqual([r['rows'] for r in self.reads(self.fixture('global-empty'))],[[[0,0,None,None,None]],[[1,0,None,None,None]]])

    def test_two_sided_batch_hand_computed(self):
        self.assertEqual(self.reads(self.fixture('two-sided-batch'))[0]['rows'],[[2,20,None],[4,40,17],[4,40,18],[None,30,None]])

    def test_self_join_and_stable_order_anchors(self):
        self.assertEqual(self.reads(self.fixture('self-join-two-roles',True))[-1]['rows'],[[2,11,11],[2,11,20],[2,20,11],[2,20,20],[None,30,None]])
        self.assertEqual(self.reads(self.fixture('stable-tie-order',True))[0]['rows'],[[1,22],[1,30]])

    def test_boolean_filter_anchor(self):
        self.assertEqual([r['rows'] for r in self.reads(self.fixture('boolean-null-filter',True))],[[[1],[None]],[[2],[None]],[[2]]])

    def test_rollback_and_id_reservation(self):
        c=self.fixture('rollback-used-id-set',True)
        replies=c['expect']['result']['results']
        self.assertEqual(replies[1],{'ok':False,'error':{'code':'UNKNOWN_ROW'}})
        self.assertEqual(replies[2],{'ok':True,'result':{'revision':1}})
        self.assertEqual(replies[3]['result']['rows'][0],[1,2,2,19,9,10])

    def test_generated_sequences_with_independent_python_relations(self):
        def key(row): return tuple((v is None,0 if v is None else v) for v in row)
        for c in extension(True,'query-null'):
            if 'generated-' not in c['id']: continue
            tables={t['name']:{i+1:list(r) for i,r in enumerate(t['rows'])} for t in c['input']['database']}
            for cmd,reply in zip(c['input']['commands'],c['expect']['result']['results']):
                if cmd['op']=='apply':
                    for ch in cmd['changes']:
                        if ch['op']=='delete': del tables[ch['table']][ch['id']]
                        else: tables[ch['table']][ch['id']]=ch['row']
                if cmd['op']!='read': continue
                if cmd['view']=='joined':
                    want=[]
                    for lk,lv in tables['l'].values():
                        matches=[rv for rk,rv in tables['r'].values() if lk is not None and rk is not None and lk==rk]
                        want.extend([[lk,lv,rv] for rv in matches] if matches else [[lk,lv,None]])
                    want.sort(key=key)
                elif cmd['view']=='groups':
                    groups={}
                    for k,v in tables['l'].values(): groups.setdefault(k,[]).append(v)
                    want=[]
                    for k,values in groups.items():
                        nonnull=[v for v in values if v is not None]
                        want.append([k,len(values),len(nonnull),sum(nonnull) if nonnull else None,min(nonnull) if nonnull else None,max(nonnull) if nonnull else None])
                    want.sort(key=lambda row:key(row[:1]))
                else:
                    vals=[v for _,v in tables['r'].values() if v is not None]
                    want=[[len(tables['r']),sum(vals) if vals else None]]
                self.assertEqual(reply['result']['rows'],want,c['id'])

    def test_scaling_expected_output_against_sqlite(self):
        for profile in ('point-updates','unrelated-table'):
            request,expect=workload(7,11,profile)
            self.assertEqual(qm.evaluate(request['input']),expect)

class DiscriminationTests(unittest.TestCase):
    def test_late_renewal_fault_survives_public_but_not_private(self):
        class LateRenewal(wm.Model):
            def command(self,c):
                if c['op']=='renew' and c['ticket'] in self.tickets:
                    t=self.tickets[c['ticket']]; _,s=self.step_for(t)
                    if self.workers[c['worker']] and t['worker']==c['worker'] and s['lease'] and s['lease']['ticket']==c['ticket'] and self.now==s['lease']['expires']:
                        s['lease']['expires']=self.add_time(self.duration)
                        return {'renewed':True}
                return super().command(c)
        def evaluate(data):
            try:
                m=LateRenewal(data); replies=[m.command(c) for c in data['commands']]
                return {'ok':True,'result':{'results':replies,'final':m.snapshot(True)}}
            except wm.Fault as e: return {'ok':False,'error':{'code':str(e)}}
        self.assertTrue(all(evaluate(c['input'])==c['expect'] for c in extension(False,'workflow-recovery')))
        c=next(c for c in extension(True,'workflow-recovery') if c['id']=='cp2-renew-at-boundary')
        self.assertNotEqual(evaluate(c['input']),c['expect'])

    def test_push_where_into_outer_join_fault(self):
        class BadPushdown(qm.Model):
            def query(self,sql):
                if 'LEFT JOIN' in sql: sql=sql.replace(' WHERE ',' AND ')
                return super().query(sql)
        def evaluate(data):
            m=BadPushdown(data); replies=[]
            for command in data['commands']:
                try: replies.append({'ok':True,'result':m.command(command)})
                except qm.Fault as e: replies.append({'ok':False,'error':{'code':str(e)}})
            return {'ok':True,'result':{'results':replies}}
        self.assertTrue(all(evaluate(c['input'])==c['expect'] for c in extension(False,'query-null')))
        c=next(c for c in extension(True,'query-null') if c['id']=='cp2-on-versus-where-plain')
        self.assertNotEqual(evaluate(c['input']),c['expect'])

if __name__=='__main__': unittest.main()
