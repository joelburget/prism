#!/usr/bin/env python3
"""Reproduce fixed public/private checkpoint-two fixtures. Evaluator only."""
from copy import deepcopy
import json
from pathlib import Path
import random
from workflow_model import evaluate as workflow, Model as Workflow, Fault
from query_model import evaluate as query

ROOT=Path(__file__).resolve().parents[1]
OLD=ROOT.parent
DATA={visibility:{task:[] for task in ('query-null','workflow-recovery')} for visibility in ('public','heldout')}

def add(task,name,description,data,private=False,expect=None):
    response=expect if expect is not None else (query(data) if task=='query-null' else workflow(data))
    DATA['heldout' if private else 'public'][task].append({'id':'cp2-'+name,'phase':'extension','description':description,'input':deepcopy(data),'expect':response})

def c(op,**kw): return {'op':op,**kw}
def wcase(name,description,commands,private=False,steps=None,**config):
    data={'steps':steps or [{'id':'s','needs':[],'amount':25}], 'workers':['a','b'], 'commands':commands,**config}
    add('workflow-recovery',name,description,data,private)

start=c('start',run='r'); observe=c('observe')
a=lambda op,**kw:c(op,worker='a',**kw)
b=lambda op,**kw:c(op,worker='b',**kw)
d=lambda n:c('deliver',ticket=n)
calla=lambda n:a('call',ticket=n)
callb=lambda n:b('call',ticket=n)
advance=lambda n:c('advance',by=n)
cancel=c('cancel',run='r')

wcase('ordinary-delivery','Call does not release dependencies; repeated calls and deliveries have distinct receipts.',[start,a('claim'),observe,calla(1),observe,calla(1),d(1),d(1),observe])
wcase('expired-success','At lease expiry a replacement replays the effect under the same attempt number.',[start,a('claim'),calla(1),advance(5),b('claim'),d(1),callb(2),d(2),observe])
wcase('parallel-dependencies','Independent branches can run concurrently; a child waits for both committed successes.',[start,a('claim'),b('claim'),calla(1),d(1),a('claim'),callb(2),d(2),a('claim'),calla(3),d(3),observe],steps=[{'id':'x','needs':[],'amount':1},{'id':'y','needs':[],'amount':2},{'id':'z','needs':['x','y'],'amount':3}])
wcase('retry-from-delivery','Retry deadline starts at delivery time rather than call or acquisition.',[start,a('claim'),calla(1),advance(3),d(1),observe,b('claim'),advance(2),b('claim'),callb(2),d(2)],steps=[{'id':'s','needs':[],'amount':25,'failures':1}])
wcase('cancel-before-effect','Cancellation immediately revokes an execute lease and recovers through lookup.',[start,a('claim'),cancel,calla(1),b('claim'),callb(2),d(2),observe])
wcase('cancel-after-effect','Cancellation preserves an applied but uncommitted effect through audited lookup.',[start,a('claim'),calla(1),cancel,d(1),b('claim'),callb(2),d(2)])
wcase('renew-and-crash','A renewal retains its ticket; a delayed response survives a worker crash.',[start,a('claim'),calla(1),advance(4),a('renew',ticket=1),a('crash'),d(1),a('restart'),advance(1),d(1),observe])
wcase('failure-drain','An exhausted branch invalidates another in-flight lease and reconciles its existing effect.',[start,a('claim'),b('claim'),callb(2),calla(1),d(1),observe,d(2),a('claim'),calla(3),d(3)],steps=[{'id':'x','needs':[],'amount':1,'failures':1},{'id':'y','needs':[],'amount':2}],max_attempts=1)
wcase('empty-claim','No eligible work consumes no ticket ID.',[a('claim'),start,a('claim'),calla(1),d(1),b('claim')])
wcase('busy-worker','An up worker cannot acquire a second live lease.',[start,a('claim'),a('claim')])
wcase('invalid-workers','Worker IDs must be unique.',[],workers=['a','a'])

wcase('stale-transient-after-success','A stale transient must not reopen a replacement success or schedule a retry.',[start,a('claim'),calla(1),advance(5),b('claim'),callb(2),d(2),d(1),observe],True,steps=[{'id':'s','needs':[],'amount':25,'failures':1}])
wcase('uncommitted-transients','Recovering lost transient responses consumes calls, not new attempt numbers.',[start,a('claim'),calla(1),advance(5),b('claim'),callb(2),advance(5),a('claim'),calla(3),d(1),d(2),d(3)],True,steps=[{'id':'s','needs':[],'amount':25,'failures':2}],max_attempts=1)
wcase('renew-at-boundary','Renewal at exact expiration fails and cannot prevent reclamation.',[start,a('claim'),advance(5),a('renew',ticket=1),b('claim'),a('renew',ticket=1),calla(1),callb(2),d(2)],True)
wcase('repeated-cancel-during-lookup','Repeated cancellation must not expire a newly acquired lookup lease.',[start,a('claim'),calla(1),cancel,b('claim'),callb(2),cancel,d(2),cancel,observe],True)
wcase('reclaimed-lookup','A lookup response can itself be lost to expiry and recovered without executing.',[start,a('claim'),cancel,b('claim'),callb(2),advance(5),a('claim'),calla(3),d(2),d(3)],True)
branches=[{'id':'x','needs':[],'amount':10},{'id':'y','needs':[],'amount':20},{'id':'z','needs':['x','y'],'amount':30}]
wcase('cancel-two-running','Mixed found/missing reconciliation drains every running branch and cancels pending children.',[start,a('claim'),b('claim'),callb(2),cancel,observe,a('claim'),b('claim'),callb(4),d(4),observe,calla(3),d(3),observe],True,steps=branches)
wcase('failure-drain-missing','Failure draining blocks an unexecuted sibling and cancellation cannot change failing to cancelling.',[start,a('claim'),b('claim'),calla(1),d(1),cancel,observe,callb(2),a('claim'),calla(3),d(3),observe],True,steps=[{'id':'x','needs':[],'amount':1,'failures':1},{'id':'y','needs':[],'amount':2}],max_attempts=1)
wcase('failure-other-run','Draining a failed run does not prevent independent runs from continuing.',[start,c('start',run='next'),a('claim'),calla(1),d(1),b('claim'),callb(2),d(2),observe],True,steps=[{'id':'s','needs':[],'amount':1,'failures':1}],max_attempts=1)
wcase('recovery-priority','Expired running work precedes pending work across run boundaries.',[start,a('claim'),c('start',run='other'),advance(5),b('claim'),callb(2),d(2),a('claim'),calla(3),d(3)],True)
wcase('dependency-not-call','An effect alone cannot release a dependent step.',[start,a('claim'),calla(1),b('claim'),observe,d(1),b('claim'),callb(2),d(2)],True,steps=[{'id':'x','needs':[],'amount':1},{'id':'y','needs':['x'],'amount':2}])
wcase('same-worker-reclaim','A worker can reclaim its own expired lease with a new ticket and unchanged attempt.',[start,a('claim'),advance(5),a('claim'),calla(1),d(1),calla(2),d(2)],True)
wcase('down-delivery-recovery','A response while down cannot commit; after expiry only the replacement may commit.',[start,a('claim'),calla(1),a('crash'),d(1),advance(5),b('claim'),a('restart'),d(1),callb(2),d(2)],True)
wcase('late-transient-preserves-deadline','Old transient delivery cannot reset a newer committed retry deadline.',[start,a('claim'),calla(1),advance(5),b('claim'),callb(2),d(2),advance(1),d(1),observe,advance(1),a('claim'),calla(3),d(3)],True,steps=[{'id':'s','needs':[],'amount':3,'failures':2}])
wcase('renewal-does-not-unexpire','Time after a successful renewal crosses the new deadline, not the original one.',[start,a('claim'),advance(4),a('renew',ticket=1),advance(1),b('claim'),advance(4),b('claim'),callb(2),d(2)],True)
wcase('unknown-ticket','Unknown delivery tickets are errors.',[d(17)],True)
wcase('wrong-owner','Ticket ownership remains its original worker even while current.',[start,a('claim'),b('call',ticket=1)],True)
wcase('down-error-precedence','Down-worker validation precedes unknown-ticket lookup.',[a('crash'),a('call',ticket=99)],True)
wcase('command-prevalidation','A malformed later command wins over an earlier runtime error.',[c('cancel',run='absent'),c('advance',by=True)],True)
wcase('claim-overflow','Acquiring a lease must check deadline arithmetic.',[advance(2_147_483_646),start,a('claim')],True)
wcase('renew-overflow','Renewal deadline overflow is an error even with a currently live lease.',[advance(2_147_483_640),start,a('claim'),advance(4),a('renew',ticket=1)],True)
wcase('retry-overflow','A committed transient validates retry deadline overflow.',[advance(2_147_483_645),start,a('claim'),calla(1),d(1)],True,steps=[{'id':'s','needs':[],'amount':1,'failures':1}],lease_duration=1,retry_delay=3)
wcase('uncalled-delivery','Delivery without a saved response is harmless and leaves the lease usable.',[start,a('claim'),d(1),calla(1),d(1)],True)
wcase('structured-keys','Different run/step tuples cannot collide through delimiter concatenation.',[c('start',run='a_b'),c('start',run='a')]+[cmd for n in range(1,5) for cmd in [a('claim'),calla(n),d(n)]],True,steps=[{'id':'c','needs':[],'amount':1},{'id':'b_c','needs':[],'amount':2}])

# Reproducible long traces; seeds are evaluator-only and fixture outputs are frozen.
for seed in (812,947,1203,2049,4097,6001,7019,9011):
    rng=random.Random(seed)
    data={'workers':['a','b','c'],'steps':[{'id':'x','needs':[],'amount':3,'failures':2},{'id':'y','needs':[],'amount':5},{'id':'z','needs':['x','y'],'amount':7,'failures':1}], 'commands':[],'lease_duration':3,'max_attempts':2,'retry_delay':2}
    m=Workflow(data)
    for name in ('r','second','third'):
        cmd=c('start',run=name); data['commands'].append(cmd); m.command(cmd)
    for _ in range(110):
        worker=rng.choice(list(m.workers)); kind=rng.choice(['claim','claim','call','deliver','advance','observe','renew','crash','restart','cancel'])
        if kind=='advance': cmd=advance(rng.randrange(4))
        elif kind=='observe': cmd=observe
        elif kind=='cancel': cmd=c(kind,run=rng.choice(list(m.runs)))
        elif kind in ('call','renew','deliver'):
            if not m.tickets: continue
            t=rng.choice(list(m.tickets.values())); cmd=c(kind,ticket=t['ticket'],**({'worker':t['worker']} if kind!='deliver' else {}))
        else: cmd=c(kind,worker=worker)
        trial=deepcopy(m)
        try: trial.command(cmd)
        except Fault: continue
        m=trial; data['commands'].append(cmd)
    # Drain uncertainty through cancellation/lookup, preserving any already applied effects.
    for worker,up in list(m.workers.items()):
        if not up:
            cmd=c('restart',worker=worker); m.command(cmd); data['commands'].append(cmd)
    for name in m.runs:
        cmd=c('cancel',run=name); m.command(cmd); data['commands'].append(cmd)
    cmd=advance(3); m.command(cmd); data['commands'].append(cmd)
    while any(s['status']=='running' for r in m.runs.values() for s in r['steps']):
        cmd=a('claim'); receipt=m.command(cmd); data['commands'].append(cmd)
        assert receipt['ticket'] is not None
        for cmd in [calla(receipt['ticket']),d(receipt['ticket'])]: m.command(cmd); data['commands'].append(cmd)
    data['commands'].append(observe)
    add('workflow-recovery',f'generated-{seed}','Seeded long interleaving combines recovery, renewal, duplicate/stale messages, retries and terminal draining.',data,True)

# Query fixture helpers. Every generated SELECT has a complete deterministic order
# or one global aggregate; SQL NULL placement is explicit where it matters.
def table(name,columns,rows):
    return {'name':name,'columns':[{'name':n,'type':t,'nullable':nullable} for n,t,nullable in columns],'rows':rows}
def create(sql,name='v',opt=True): return c('create',view=name,sql=sql,optimize=opt)
def read(name='v'): return c('read',view=name)
def change(op,table,id,row=None): return c(op,table=table,id=id,**({'row':row} if op!='delete' else {}))
def apply(*changes): return c('apply',changes=list(changes))
def qcase(name,description,database,commands,private=False):
    # Pair the whole trace, not only its initial query, across both optimizer modes.
    for optimize in (False,True):
        ops=deepcopy(commands)
        for op in ops:
            if op['op']=='create': op['optimize']=optimize
        add('query-null',name+('-optimized' if optimize else '-plain'),description,{'database':database,'commands':ops},private)

l=table('l',[('k','int',True),('v','int',True)],[[1,10],[2,20],[None,30]])
r=table('r',[('k','int',True),('w','int',True)],[[1,7],[1,8],[None,9]])
join='SELECT l.k AS k, l.v AS v, r.w AS w FROM l LEFT JOIN r ON l.k = r.k ORDER BY k NULLS LAST, v NULLS LAST, w NULLS LAST'
qcase('last-and-first-match','Removing a final join match restores padding; its first replacement retracts padding.',[l,r],[create(join),read(),apply(change('delete','r',1)),read(),apply(change('delete','r',2)),read(),apply(change('insert','r',4,[1,11])),read()])
t=table('t',[('x','int',True)],[[4],[4],[None]])
qcase('distinct-multiplicity','DISTINCT retains a duplicate until the last occurrence is removed, including NULL.',[t],[create('SELECT DISTINCT x AS x FROM t ORDER BY x NULLS LAST'),read(),apply(change('delete','t',1)),read(),apply(change('delete','t',2)),read(),apply(change('insert','t',4,[None])),apply(change('delete','t',3)),read()])
agg='SELECT k AS k, COUNT(*) AS n, COUNT(v) AS nv, SUM(v) AS s, MIN(v) AS lo, MAX(v) AS hi FROM l GROUP BY k ORDER BY k NULLS LAST'
qcase('aggregate-retraction','Updating group keys and deleting extremal values retracts the correct aggregate contribution.',[l],[create(agg),read(),apply(change('insert','l',4,[1,5])),read(),apply(change('update','l',1,[2,None])),read(),apply(change('delete','l',4)),read()])
qcase('atomic-rollback','A later error rolls back earlier row changes and ID reservations in the same batch.',[l],[create(agg),apply(change('insert','l',4,[1,4]),change('delete','l',99)),read(),apply(change('insert','l',4,[1,5])),read()])
qcase('two-sided-batch','Changing both sides of a join in one batch must yield the final cross product once.',[l,r],[create(join),apply(change('update','l',1,[4,40]),change('update','r',1,[4,17]),change('update','r',2,[4,18])),read()])
qcase('global-empty','Global aggregate retains one empty-input row and correct NULL results.',[table('t',[('x','int',True)],[[9]])],[create('SELECT COUNT(*) AS n, COUNT(x) AS nx, SUM(x) AS s, MIN(x) AS lo, MAX(x) AS hi FROM t'),apply(change('delete','t',1)),read(),apply(change('insert','t',2,[None])),read()])
qcase('sort-window','Updates and deletions move rows across ORDER BY / OFFSET / LIMIT boundaries.',[table('t',[('x','int',True)],[[2],[4],[6],[8]])],[create('SELECT x AS x FROM t ORDER BY x DESC NULLS LAST LIMIT 2 OFFSET 1'),read(),apply(change('update','t',1,[10])),read(),apply(change('delete','t',4)),read()])
qcase('view-lifecycle','Drop/recreate binds a new query while revisions stay global.',[l,r],[create(agg),apply(change('update','r',1,[2,99])),read(),c('drop',view='v'),read(),create(join),read()])
qcase('used-row-id','Delete reserves its old ID; failed inserts do not advance revision.',[t],[create('SELECT x AS x FROM t ORDER BY x NULLS LAST'),apply(change('delete','t',1)),apply(change('insert','t',1,[8])),read()])
qcase('invalid-row-atomicity','Wrong row types fail the entire batch without changing view state.',[l],[create(agg),apply(change('update','l',1,[1,77]),change('insert','l',4,[True,6])),read()])

qcase('null-join-transition','NULL-to-value and value-to-NULL changes affect padding, matches and multiplicities.',[l,r],[create(join),apply(change('update','r',3,[2,90]),change('update','l',1,[None,10])),read(),apply(change('update','l',3,[2,30]),change('update','r',1,[None,7])),read()],True)
qcase('having-thresholds','HAVING membership changes as aggregates cross the threshold in either direction.',[l],[create('SELECT k AS k, SUM(v) AS s FROM l GROUP BY k HAVING SUM(v) > 15 ORDER BY k NULLS LAST'),read(),apply(change('insert','l',4,[1,8]),change('update','l',2,[2,1])),read(),apply(change('delete','l',4)),read()],True)
qcase('null-group-and-all-null','NULL group keys merge; deleting its final non-NULL value leaves NULL aggregates.',[table('l',[('k','int',True),('v','int',True)],[[None,None],[None,4],[1,9]])],[create(agg),apply(change('delete','l',2)),read(),apply(change('update','l',3,[None,7])),read(),apply(change('delete','l',1),change('delete','l',3)),read()],True)
z=table('z',[('k','int',True),('q','int',True)],[[7,70],[8,80],[None,90]])
chain='SELECT l.k AS k, r.w AS w, z.q AS q FROM l LEFT JOIN r ON l.k = r.k LEFT JOIN z ON r.w = z.k ORDER BY k NULLS LAST, w NULLS LAST, q NULLS LAST'
qcase('chained-outer-joins','Updating downstream matches and upstream keys retracts all affected join chains.',[l,r,z],[create(chain),read(),apply(change('delete','r',1),change('update','z',1,[8,71]),change('update','l',2,[1,20])),read(),apply(change('insert','r',4,[2,70]),change('update','l',1,[2,10])),read()],True)
qcase('on-versus-where','WHERE rejects unmatched padding whereas equivalent ON predicates preserve it.',[l,r],[create('SELECT l.k AS k, r.w AS w FROM l LEFT JOIN r ON l.k = r.k WHERE r.w > 7 ORDER BY k NULLS LAST, w NULLS LAST','where-view'),create('SELECT l.k AS k, r.w AS w FROM l LEFT JOIN r ON l.k = r.k AND r.w > 7 ORDER BY k NULLS LAST, w NULLS LAST','on-view'),apply(change('delete','r',2)),read('where-view'),read('on-view'),apply(change('insert','r',4,[2,12])),read('where-view'),read('on-view')],True)
qcase('net-zero-batch','A net-zero batch advances revision without corrupting cached counts.',[l],[create(agg),apply(change('insert','l',4,[1,4]),change('delete','l',4)),read(),apply(change('insert','l',4,[1,4])),read()],True)
qcase('batch-shape-precedence','All structural checks precede runtime row lookup, and failure is atomic.',[l],[create(agg),apply(change('delete','l',999),{'op':'insert','table':'l','id':True,'row':[1,2]}),read()],True)
qcase('row-error-precedence','Row validation precedes duplicate or absent ID checks.',[l],[create(agg),apply(change('insert','l',1,[1,'bad'])),apply(change('update','l',999,[1,True])),read()],True)
qcase('rollback-used-id-set','Insert/delete before a failing change must roll back even the used-ID set.',[l],[create(agg),apply(change('insert','l',4,[1,8]),change('delete','l',4),change('delete','l',999)),apply(change('insert','l',4,[1,9])),read()],True)
qcase('multiple-view-isolation','Several differently grouped and filtered views update independently over one batch.',[l,r],[create(agg,'groups'),create(join,'joined'),create('SELECT COUNT(*) AS n FROM r','counted'),apply(change('delete','l',2),change('update','r',2,[2,90])),read('groups'),read('joined'),read('counted'),c('drop',view='groups'),apply(change('insert','r',4,[1,4])),read('joined'),read('counted')],True)
qcase('inner-join-bag','Deleting a duplicate right row removes one contribution per matching left row.',[table('l',[('k','int',True),('v','int',True)],[[1,10],[1,10]]),r],[create('SELECT l.k AS k, COUNT(*) AS n, SUM(r.w) AS s FROM l INNER JOIN r ON l.k = r.k GROUP BY l.k ORDER BY k NULLS LAST'),read(),apply(change('delete','r',1)),read(),apply(change('delete','l',1)),read()],True)
qcase('stable-tie-order','An update retains encounter position; insertion appends even with a smaller private ID.',[table('t',[('k','int',False),('v','int',False)],[[1,10],[1,20],[1,30]])],[create('SELECT k AS k, v AS v FROM t ORDER BY k LIMIT 2'),apply(change('delete','t',1)),apply(change('insert','t',9,[1,90])),apply(change('insert','t',4,[1,40])),apply(change('update','t',2,[1,22])),read()],True)
qcase('text-extrema','Retracting a text minimum and maximum exposes the correct replacement.',[table('t',[('x','text',True)],[['a'],['z'],['m'],[None]])],[create('SELECT MIN(x) AS lo, MAX(x) AS hi FROM t'),apply(change('delete','t',1),change('delete','t',2)),read(),apply(change('update','t',3,[None])),read()],True)
qcase('creation-after-mutations','New views build from current base rows and can share a name previously dropped.',[l],[apply(change('update','l',1,[1,99])),create(agg),read(),c('drop',view='v'),apply(change('delete','l',1)),create('SELECT COUNT(*) AS n FROM l'),read()],True)
qcase('view-error-isolation','Failed view creation/drop/read do not corrupt existing views or revision.',[l],[create(agg),create('SELECT COUNT(*) AS n FROM l'),c('drop',view='missing'),read('missing'),read()],True)
qcase('same-id-across-tables','Row-ID namespaces are per table; same IDs in one atomic batch are independent.',[l,r],[create(join),apply(change('insert','l',4,[3,33]),change('insert','r',4,[3,44])),read()],True)
qcase('coalesce-retraction','Per-row COALESCE and nullable arithmetic remain correct as rows change between NULL and values.',[l],[create('SELECT k AS k, SUM(COALESCE(v, 5) + 1) AS s FROM l GROUP BY k ORDER BY k NULLS LAST'),apply(change('update','l',1,[1,None]),change('insert','l',4,[1,2])),read(),apply(change('delete','l',1)),read()],True)
qcase('self-join-two-roles','One base-table change affects both aliases of a self join, including their cross term.',[l],[create('SELECT a.k AS k, a.v AS av, b.v AS bv FROM l AS a LEFT JOIN l AS b ON a.k = b.k ORDER BY k NULLS LAST, av NULLS LAST, bv NULLS LAST'),apply(change('insert','l',4,[1,40])),read(),apply(change('update','l',1,[2,11]),change('delete','l',4)),read()],True)
qcase('boolean-null-filter','Boolean TRUE/FALSE/NULL updates change predicate membership under three-valued logic.',[table('t',[('x','int',True),('flag','bool',True)],[[1,True],[2,False],[None,None]])],[create('SELECT x AS x FROM t WHERE flag OR x IS NULL ORDER BY x NULLS LAST'),read(),apply(change('update','t',1,[1,None]),change('update','t',2,[2,True])),read(),apply(change('update','t',3,[3,None])),read()],True)
qcase('duplicate-extrema','Retracting one of two equal minima must retain the minimum until its last contribution disappears.',[table('t',[('x','int',True)],[[2],[2],[9]])],[create('SELECT MIN(x) AS lo, MAX(x) AS hi FROM t'),apply(change('delete','t',1)),read(),apply(change('delete','t',2)),read()],True)

for seed in (313,719,1229,2027,4001,5003):
    rng=random.Random(seed); database=deepcopy([l,r]); live={'l':{1,2,3},'r':{1,2,3}}; next_id={'l':4,'r':4}
    ops=[create(join,'joined'),create(agg,'groups'),create('SELECT COUNT(*) AS n, SUM(w) AS s FROM r','total')]
    for index in range(36):
        batch=[]
        for name in rng.sample(['l','r'],rng.randint(1,2)):
            kind=rng.choice(['insert','update','delete']) if live[name] else 'insert'
            if kind=='insert': rid=next_id[name]; next_id[name]+=1; live[name].add(rid)
            else:
                rid=rng.choice(sorted(live[name]))
                if kind=='delete': live[name].remove(rid)
            row=[rng.choice([None,1,2,3]),rng.choice([None,-3,0,5,11])]
            batch.append(change(kind,name,rid,row))
        ops.append(apply(*batch))
        if index%3==0: ops += [read('joined'),read('groups'),read('total')]
    ops += [read('joined'),read('groups'),read('total')]
    qcase(f'generated-{seed}','Seeded multi-table batches combine join-key movement, NULLs, bag multiplicity and aggregate retractions.',database,ops,True)

def write():
    for visibility,tasks in DATA.items():
        base=ROOT if visibility=='public' else ROOT/'evaluator'/'heldout'
        for task,new in tasks.items():
            old_path=(OLD if visibility=='public' else OLD/'heldout')/task/'cases.json'
            old=json.loads(old_path.read_text())['cases']
            baseline=[{**case,'id':'cp1-'+case['id'],'phase':'baseline'} for case in old]
            out=base/task; out.mkdir(parents=True,exist_ok=True)
            (out/'cases.json').write_text(json.dumps({'schema_version':1,'task':task,'cases':baseline+new},indent=2)+'\n')
            if visibility=='heldout':
                (out/'COVERAGE.md').write_text('# Private checkpoint-two coverage\n\nAll checkpoint-one held-out cases remain baseline regressions. New cases:\n\n'+'\n'.join('- **'+case['id']+'**: '+case['description'] for case in new)+'\n')
            print(f'{visibility}/{task}: {len(baseline)} regression + {len(new)} extension')
    for task in DATA['public']:
        (ROOT/task/'PREVIOUS.md').write_bytes((OLD/task/'PROBLEM.md').read_bytes())

if __name__=='__main__': write()
