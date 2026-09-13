#!/usr/bin/env python3
"""Agent-authored differential test for incrementally maintained views.

Random databases, random queries covering the whole SQL subset, and random
batches of inserts/updates/deletes; after every batch each view's read is
compared against a fresh checkpoint-one query on the same final table
contents, in both optimizer modes. Run: python3 test_views.py [seed] [rounds]"""
import copy
import json
import random
import sys

from model import validate
from parser import Parser
from binder import bind
from engine import optimize, execute
from views import Store
import subprocess, os

HERE = os.path.dirname(os.path.abspath(__file__))


def fresh(database, sql, optimized):
    db = {name: {'name': name, 'columns': t['columns'], 'rows': [list(r) for r in t['live'].values()]} for name, t in database.items()}
    plan = bind(Parser(sql).parse(), db)
    return execute(optimize(plan) if optimized else plan)


def gen_value(rng, col):
    if col['nullable'] and rng.random() < 0.25: return None
    if col['type'] == 'int': return rng.randint(-3, 3)
    if col['type'] == 'bool': return rng.random() < 0.5
    return rng.choice(['a', 'b', 'c'])


def gen_table(rng, name, n):
    cols = [{'name': 'k', 'type': 'int', 'nullable': rng.random() < 0.7},
            {'name': 'v', 'type': 'int', 'nullable': rng.random() < 0.7},
            {'name': 's', 'type': 'text', 'nullable': rng.random() < 0.5},
            {'name': 'b', 'type': 'bool', 'nullable': rng.random() < 0.5}]
    return {'name': name, 'columns': cols, 'rows': [[gen_value(rng, c) for c in cols] for _ in range(n)]}


def gen_pred(rng, aliases):
    a = rng.choice(aliases)
    b = rng.choice(aliases)
    choices = [f'{a}.k = {b}.k', f'{a}.v > {rng.randint(-2, 2)}', f'{a}.k IS NULL', f'{a}.k IS NOT NULL', f'{a}.b',
               f'NOT {a}.b', f'{a}.s = \'a\'', f'{a}.v + {b}.v <> 0', f'COALESCE({a}.k, 0) = {rng.randint(-2, 2)}',
               f'{a}.k = {a}.k', f'{a}.k <> {a}.k', 'TRUE', 'NULL', f'{a}.v * 2 >= {b}.k']
    p = rng.choice(choices)
    if rng.random() < 0.3: p = f'({p}) {rng.choice(["AND", "OR"])} ({rng.choice(choices)})'
    return p


def gen_query(rng, tables):
    n = rng.choice([1, 1, 2, 2, 3])
    names = [rng.choice(tables) for _ in range(n)]
    aliases = [f't{i}' for i in range(n)]
    sql = f'FROM {names[0]} AS t0'
    for i in range(1, n):
        kind = rng.choice(['JOIN', 'LEFT JOIN', 'INNER JOIN', 'LEFT OUTER JOIN'])
        left = rng.choice(aliases[:i])
        on = rng.choice([f'{left}.k = t{i}.k', f'{left}.k = t{i}.k AND t{i}.v > 0', f'{left}.v < t{i}.v', f'{left}.k = t{i}.k AND t{i}.b',
                         f'{left}.k = t{i}.v', f'{left}.s = t{i}.s AND {left}.k = t{i}.k', f'{left}.k + 1 = t{i}.k', 'TRUE'])
        sql += f' {kind} {names[i]} AS t{i} ON {on}'
    if rng.random() < 0.6: sql += ' WHERE ' + gen_pred(rng, aliases)
    aggregate = rng.random() < 0.5
    if aggregate:
        keys = []
        if rng.random() < 0.75:
            keys = rng.sample([f'{a}.{c}' for a in aliases for c in ('k', 's', 'b')], rng.choice([1, 1, 2]))
        a = rng.choice(aliases)
        items = [f'{k} AS g{i}' for i, k in enumerate(keys)]
        items += rng.sample([f'COUNT(*) AS n', f'COUNT({a}.v) AS nv', f'SUM({a}.v) AS sv', f'MIN({a}.v) AS lo', f'MAX({a}.s) AS hs',
                             f'MIN({a}.s) AS ls', f'SUM({a}.v) + COUNT(*) AS mix', f'COALESCE(SUM({a}.k), -1) AS cs'], rng.randint(1, 3))
        select = ', '.join(items)
        tail = (' GROUP BY ' + ', '.join(keys)) if keys else ''
        if rng.random() < 0.4: tail += ' HAVING ' + rng.choice([f'COUNT(*) > 1', f'SUM({a}.v) IS NOT NULL', f'MIN({a}.v) < 0', f'COUNT({a}.v) = COUNT(*)'])
    else:
        a = rng.choice(aliases)
        b = rng.choice(aliases)
        items = rng.sample([f'{a}.k AS x', f'{b}.v AS y', f'{a}.s AS s', f'{a}.b AS b', f'{a}.v + {b}.v AS sum', f'{a}.k IS NULL AS nk',
                            f'COALESCE({a}.v, {b}.v, 0) AS c'], rng.randint(1, 3))
        select = ', '.join(items)
        tail = ''
    outputs = [item.split(' AS ')[1] for item in items]
    sql = f'SELECT {"DISTINCT " if rng.random() < 0.3 else ""}{select} {sql}{tail}'
    if rng.random() < 0.7:
        order = []
        for alias in rng.sample(outputs, rng.randint(1, len(outputs))):
            order.append(alias + rng.choice(['', ' ASC', ' DESC']) + rng.choice(['', '', ' NULLS FIRST', ' NULLS LAST']))
        sql += ' ORDER BY ' + ', '.join(order)
    if rng.random() < 0.4:
        sql += f' LIMIT {rng.randint(0, 6)}'
        if rng.random() < 0.5: sql += f' OFFSET {rng.randint(0, 3)}'
    return sql


def gen_batch(rng, database, next_ids):
    changes = []
    live = {name: list(t['live']) for name, t in database.items()}
    for _ in range(rng.randint(1, 6)):
        name = rng.choice(list(database))
        t = database[name]
        op = rng.choice(['insert', 'update', 'update', 'delete', 'delete'])
        if op == 'insert' or not live[name]:
            rid = next_ids[name]
            next_ids[name] += 1
            live[name].append(rid)
            changes.append({'op': 'insert', 'table': name, 'id': rid, 'row': [gen_value(rng, c) for c in t['columns']]})
        elif op == 'update':
            changes.append({'op': 'update', 'table': name, 'id': rng.choice(live[name]), 'row': [gen_value(rng, c) for c in t['columns']]})
        else:
            rid = rng.choice(live[name])
            live[name].remove(rid)
            changes.append({'op': 'delete', 'table': name, 'id': rid})
    return changes


def run_round(seed, verbose=False):
    rng = random.Random(seed)
    tables = [gen_table(rng, name, rng.randint(0, 6)) for name in ('p', 'q', 'r')[:rng.randint(1, 3)]]
    request = {'protocol_version': 1, 'task': 'query-null', 'input': {'database': copy.deepcopy(tables), 'commands': []}}
    database, _, _ = validate(request)
    store = Store(database)
    next_ids = {t['name']: len(t['rows']) + 1 for t in tables}
    views = {}
    failures = 0
    for i in range(rng.randint(1, 5)):
        sql = gen_query(rng, [t['name'] for t in tables])
        optimized = rng.random() < 0.5
        try:
            fresh(database, sql, optimized)
        except Exception as e:   # invalid generated query: create must fail identically
            reply = store.run({'op': 'create', 'view': f'v{i}', 'sql': sql, 'optimize': optimized})
            if reply.get('ok'):
                print('FAIL create accepted invalid query', seed, sql, e)
                failures += 1
            continue
        reply = store.run({'op': 'create', 'view': f'v{i}', 'sql': sql, 'optimize': optimized})
        assert reply['ok'], (reply, sql)
        views[f'v{i}'] = (sql, optimized)
    for step in range(rng.randint(1, 12)):
        batch = gen_batch(rng, database, next_ids)
        reply = store.run({'op': 'apply', 'changes': batch})
        assert reply['ok'], (reply, batch)
        for name, (sql, optimized) in views.items():
            got = store.run({'op': 'read', 'view': name})['result']
            want = fresh(database, sql, optimized)
            if got['rows'] != want['rows'] or got['columns'] != want['columns']:
                failures += 1
                print('FAIL seed', seed, 'step', step, 'optimize', optimized, '\n ', sql, '\n  tables', {n: list(t['live'].items()) for n, t in database.items()}, '\n  batch', batch, '\n  got ', json.dumps(got['rows']), '\n  want', json.dumps(want['rows']))
    return failures


def protocol_checks():
    """Command-shape and lifecycle checks through the real entrypoint."""
    def run(commands, database=None):
        db = database if database is not None else [{'name': 't', 'columns': [{'name': 'x', 'type': 'int', 'nullable': True}], 'rows': [[1], [2]]}]
        request = {'protocol_version': 1, 'task': 'query-null', 'input': {'database': db, 'commands': commands}}
        out = subprocess.run(['python3', os.path.join(HERE, 'main.py')], input=json.dumps(request), capture_output=True, text=True)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)
    def codes(commands):
        r = run(commands)
        return [x['result'] if x['ok'] else x['error']['code'] for x in r['result']['results']]
    failures = 0
    def expect(got, want):
        nonlocal failures
        if got != want:
            failures += 1
            print('FAIL protocol\n  got ', json.dumps(got), '\n  want', json.dumps(want))
    expect(codes([{'op': 'nope'}, 'x', {'op': 'read'}, {'op': 'read', 'view': 'V'}, {'op': 'read', 'view': 'v'}, {'op': 'drop', 'view': 'v'}]),
           ['INVALID_COMMAND', 'INVALID_COMMAND', 'INVALID_COMMAND', 'INVALID_COMMAND', 'UNKNOWN_VIEW', 'UNKNOWN_VIEW'])
    expect(codes([{'op': 'create', 'view': 'v', 'sql': 'SELECT x AS x FROM t', 'optimize': 1},
                  {'op': 'create', 'view': 'v', 'sql': 'SELECT x AS x FROM t', 'optimize': True, 'extra': 1},
                  {'op': 'create', 'view': 'v', 'sql': 'SELECT x FROM t', 'optimize': True},
                  {'op': 'create', 'view': 'v', 'sql': 'SELECT y AS y FROM t', 'optimize': True},
                  {'op': 'create', 'view': 'v', 'sql': 'SELECT x AS x FROM u', 'optimize': True},
                  {'op': 'create', 'view': 'v', 'sql': 'SELECT x AS x FROM t', 'optimize': True},
                  {'op': 'create', 'view': 'v', 'sql': 'SELECT x AS x FROM t', 'optimize': False}]),
           ['INVALID_COMMAND', 'INVALID_COMMAND', 'PARSE_ERROR', 'UNKNOWN_COLUMN', 'UNKNOWN_TABLE', {'view': 'v', 'revision': 0}, 'VIEW_EXISTS'])
    expect(codes([{'op': 'apply', 'changes': []}, {'op': 'apply', 'changes': {}}, {'op': 'apply'},
                  {'op': 'apply', 'changes': [{'op': 'delete', 'table': 't', 'id': 1}, {'op': 'delete', 'table': 't', 'id': 0}]},
                  {'op': 'apply', 'changes': [{'op': 'delete', 'table': 't', 'id': True}]},
                  {'op': 'apply', 'changes': [{'op': 'delete', 'table': 't', 'id': 1, 'row': [1]}]},
                  {'op': 'apply', 'changes': [{'op': 'insert', 'table': 't', 'id': 3}]},
                  {'op': 'apply', 'changes': [{'op': 'insert', 'table': 't', 'id': 3, 'row': 5}]},
                  {'op': 'apply', 'changes': [{'op': 'insert', 'table': 'T', 'id': 3, 'row': [5]}]},
                  {'op': 'apply', 'changes': [{'op': 'insert', 'table': 'select', 'id': 3, 'row': [5]}]},
                  {'op': 'apply', 'changes': [{'op': 'insert', 'table': 'u', 'id': 3, 'row': [5]}]},
                  {'op': 'apply', 'changes': [{'op': 'insert', 'table': 't', 'id': 3, 'row': [5, 6]}]},
                  {'op': 'apply', 'changes': [{'op': 'insert', 'table': 't', 'id': 3, 'row': [True]}]},
                  {'op': 'apply', 'changes': [{'op': 'insert', 'table': 't', 'id': 3, 'row': [1000000001]}]},
                  {'op': 'apply', 'changes': [{'op': 'insert', 'table': 't', 'id': 2, 'row': [5]}]},
                  {'op': 'apply', 'changes': [{'op': 'update', 'table': 't', 'id': 3, 'row': [5]}]},
                  {'op': 'apply', 'changes': [{'op': 'insert', 'table': 't', 'id': 3, 'row': [5]}, {'op': 'update', 'table': 't', 'id': 3, 'row': [6]}, {'op': 'delete', 'table': 't', 'id': 3}]},
                  {'op': 'apply', 'changes': [{'op': 'insert', 'table': 't', 'id': 3, 'row': [5]}]},
                  {'op': 'apply', 'changes': [{'op': 'insert', 'table': 't', 'id': 4, 'row': [None]}]},
                  {'op': 'create', 'view': 'v', 'sql': 'SELECT x AS x FROM t', 'optimize': True},
                  {'op': 'read', 'view': 'v'}]),
           ['INVALID_COMMAND', 'INVALID_COMMAND', 'INVALID_COMMAND', 'INVALID_COMMAND', 'INVALID_COMMAND', 'INVALID_COMMAND', 'INVALID_COMMAND', 'INVALID_COMMAND',
            'INVALID_COMMAND', 'UNKNOWN_TABLE', 'UNKNOWN_TABLE', 'INVALID_ROW', 'INVALID_ROW', 'INVALID_ROW', 'ROW_ID_USED', 'UNKNOWN_ROW', {'revision': 1}, 'ROW_ID_USED',
            {'revision': 2}, {'view': 'v', 'revision': 2}, {'revision': 2, 'columns': ['x'], 'rows': [[1], [2], [None]]}])
    # Envelope: mixing or missing modes, non-array commands, unknown fields.
    def top(inp):
        out = subprocess.run(['python3', os.path.join(HERE, 'main.py')], input=json.dumps({'protocol_version': 1, 'task': 'query-null', 'input': inp}), capture_output=True, text=True)
        return json.loads(out.stdout)
    for inp in [{'database': [], 'commands': [], 'queries': []}, {'database': []}, {'commands': []}, {'database': [], 'commands': {}}, {'database': [], 'commands': None}]:
        expect(top(inp), {'ok': False, 'error': {'code': 'INVALID_INPUT'}})
    expect(top({'database': [], 'commands': []}), {'ok': True, 'result': {'results': []}})
    expect(top({'database': [], 'queries': []}), {'ok': True, 'result': {'results': []}})
    # Invalid database is reported before commands.
    expect(top({'database': [{'name': 't', 'columns': [{'name': 'x', 'type': 'int', 'nullable': False}], 'rows': [[None]]}], 'commands': [{'op': 'read', 'view': 'v'}]}),
           {'ok': False, 'error': {'code': 'INVALID_INPUT'}})
    # Reads are snapshots: a later batch does not alter an earlier reply.
    r = run([{'op': 'create', 'view': 'v', 'sql': 'SELECT x AS x FROM t', 'optimize': False}, {'op': 'read', 'view': 'v'},
             {'op': 'apply', 'changes': [{'op': 'update', 'table': 't', 'id': 1, 'row': [9]}]}, {'op': 'read', 'view': 'v'}])['result']['results']
    expect([r[1]['result']['rows'], r[3]['result']['rows']], [[[1], [2]], [[9], [2]]])
    return failures


if __name__ == '__main__':
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 300
    failures = protocol_checks()
    for s in range(seed, seed + rounds): failures += run_round(s)
    print('views: FAILED' if failures else f'views: ok ({rounds} random rounds)')
    sys.exit(1 if failures else 0)
