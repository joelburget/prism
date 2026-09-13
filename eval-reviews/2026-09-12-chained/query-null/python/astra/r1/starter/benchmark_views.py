"""Local scaling smoke workload; no calibrated acceptance threshold."""
import time
from test_extension import table
from views import commands
for n in (2000, 8000):
    db = {name: table(name, [('k', 'int', False), ('v', 'int', True)], [[i, i] for i in range(n)]) for name in ('aa', 'bb')}
    cmds = [
        {'op': 'create', 'view': 'joined', 'sql': 'SELECT aa.k AS k, SUM(bb.v) AS s FROM aa LEFT JOIN bb ON aa.k = bb.k GROUP BY aa.k ORDER BY s DESC LIMIT 10', 'optimize': True},
        {'op': 'create', 'view': 'global', 'sql': 'SELECT COUNT(v) AS n, SUM(v) AS s FROM aa', 'optimize': False},
        {'op': 'create', 'view': 'unchanged', 'sql': 'SELECT COUNT(*) AS n FROM bb', 'optimize': True},
    ]
    for i in range(300):
        rid = i % n + 1
        cmds.append({'op': 'apply', 'changes': [{'op': 'update', 'table': 'aa', 'id': rid, 'row': [rid - 1, -i]}]})
        cmds.append({'op': 'read', 'view': 'global'})
    start = time.perf_counter()
    replies = commands(db, cmds)
    elapsed = time.perf_counter() - start
    assert all(r['ok'] for r in replies)
    assert replies[-1]['result']['rows'] == [[n, n*(n-1)//2 - 2*sum(range(300))]]
    print(f'{2*n} initial rows, 3 views, 300 updates/reads: {elapsed:.3f}s')
