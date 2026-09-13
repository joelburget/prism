#!/usr/bin/env python3
"""Agent-authored checks: fixed edge cases plus a randomized differential test
that compares optimized against unoptimized execution. Run: python3 test_null.py"""
import copy
import json
import random
import subprocess
import sys
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def run(database, sql, optimize=True):
    request = {'protocol_version': 1, 'task': 'query-null', 'input': {'database': database, 'queries': [{'sql': sql, 'optimize': optimize}]}}
    out = subprocess.run(['python3', os.path.join(HERE, 'main.py')], input=json.dumps(request), capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def rows(result):
    return result['result']['results'][0]['rows']


def col(name, type, nullable=False):
    return {'name': name, 'type': type, 'nullable': nullable}


L = {'name': 'l', 'columns': [col('id', 'int'), col('k', 'int', True)], 'rows': [[1, 1], [2, None], [3, 3], [4, 4]]}
R = {'name': 'r', 'columns': [col('id', 'int', True), col('v', 'int', True), col('b', 'bool', True)], 'rows': [[1, 10, True], [1, None, None], [2, 20, False], [None, 99, True], [3, 30, None]]}
S = {'name': 's', 'columns': [col('id', 'int'), col('name', 'text')], 'rows': [[10, 'ten'], [20, 'twenty'], [30, 'thirty']]}
DB = [L, R, S]


def expect(sql, expected, optimize=True):
    got = run(DB, sql, optimize)
    if got != expected:
        print('FAIL', sql, '\n  expected', json.dumps(expected), '\n  got     ', json.dumps(got))
        return False
    return True


def error(code):
    return {'ok': False, 'error': {'code': code}}


def ok(columns, rows):
    return {'ok': True, 'result': {'results': [{'columns': columns, 'rows': rows}]}}


FIXED = [
    # parse errors
    ("SELECT id IS 10 AS x FROM l", error('PARSE_ERROR')),
    ("SELECT id = 1 IS NULL AS x FROM l", error('PARSE_ERROR')),
    ("SELECT COALESCE(id) AS x FROM l", error('PARSE_ERROR')),
    ("SELECT id AS first FROM l", error('PARSE_ERROR')),
    ("SELECT id AS x FROM l LEFT OUTER JOIN r ON l.id = r.id ORDER BY x NULLS", error('PARSE_ERROR')),
    ("SELECT id AS x FROM l ORDER BY x NULLS MIDDLE", error('PARSE_ERROR')),
    # binding / type errors
    ("SELECT id AS x FROM l WHERE NULL", ok(['x'], [])),
    ("SELECT id AS x FROM l WHERE 1", error('TYPE_ERROR')),
    ("SELECT NULL + 'a' AS x FROM l", error('TYPE_ERROR')),
    ("SELECT COALESCE(NULL, id, 'a') AS x FROM l", error('TYPE_ERROR')),
    ("SELECT COALESCE(NULL, NULL) AS x FROM l LIMIT 1", ok(['x'], [[None]])),
    ("SELECT id < NULL AS x FROM l LIMIT 1", ok(['x'], [[None]])),
    ("SELECT TRUE < NULL AS x FROM l", error('TYPE_ERROR')),
    ("SELECT MIN(NULL) AS x, SUM(NULL) AS y, COUNT(NULL) AS z FROM l", ok(['x', 'y', 'z'], [[None, None, 0]])),
    ("SELECT SUM(k) AS s, MIN(k) AS lo, MAX(k) AS hi, COUNT(k) AS c FROM l WHERE k IS NULL", ok(['s', 'lo', 'hi', 'c'], [[None, None, None, 0]])),
    ("SELECT k AS k FROM l ORDER BY k DESC NULLS LAST", ok(['k'], [[4], [3], [1], [None]])),
    ("SELECT k AS k FROM l ORDER BY k asc nulls first", ok(['k'], [[None], [1], [3], [4]])),
    ("SELECT id AS x FROM l WHERE k IS NOT NULL AND k <> k", ok(['x'], [])),
    ("SELECT id AS x FROM l WHERE id = id", ok(['x'], [[1], [2], [3], [4]])),
    ("SELECT id AS x FROM l WHERE k = k", ok(['x'], [[1], [3], [4]])),
    ("SELECT id AS x FROM l WHERE NOT (k IS NULL)", ok(['x'], [[1], [3], [4]])),
    ("SELECT id AS x FROM l WHERE NOT k IS NULL", ok(['x'], [[1], [3], [4]])),
    ("SELECT id AS x FROM l WHERE k + 1 IS NULL", ok(['x'], [[2]])),
    # left join where/on semantics and pushdown legality
    ("SELECT l.id AS id, r.v AS v FROM l LEFT JOIN r ON l.id = r.id WHERE r.id IS NULL", ok(['id', 'v'], [[4, None]])),
    ("SELECT l.id AS id, r.v AS v FROM l LEFT JOIN r ON l.id = r.id WHERE r.v IS NULL", ok(['id', 'v'], [[1, None], [4, None]])),
    ("SELECT l.id AS id, r.v AS v FROM l LEFT JOIN r ON l.id = r.id WHERE r.v IS NULL OR r.v > 15", ok(['id', 'v'], [[1, None], [2, 20], [3, 30], [4, None]])),
    ("SELECT l.id AS id, r.v AS v FROM l LEFT JOIN r ON l.id = r.id WHERE r.v > 15 AND r.b IS NULL", ok(['id', 'v'], [[3, 30]])),
    ("SELECT l.id AS id, r.v AS v FROM l LEFT JOIN r ON l.id = r.id WHERE l.id > 2", ok(['id', 'v'], [[3, 30], [4, None]])),
    ("SELECT l.id AS id, r.v AS v FROM l LEFT JOIN r ON l.id = r.id WHERE COALESCE(r.v, 0) = 0", ok(['id', 'v'], [[1, None], [4, None]])),
    ("SELECT l.id AS id, r.v AS v FROM l LEFT JOIN r ON l.id = r.id WHERE r.b OR NOT r.b", ok(['id', 'v'], [[1, 10], [2, 20]])),
    ("SELECT l.id AS id, r.id = r.id AS same FROM l LEFT JOIN r ON l.id = r.id AND r.v > 100", ok(['id', 'same'], [[1, None], [2, None], [3, None], [4, None]])),
    ("SELECT l.id AS id, s.name AS name FROM l LEFT JOIN r ON l.id = r.id LEFT JOIN s ON r.v = s.id WHERE s.name IS NULL", ok(['id', 'name'], [[1, None], [4, None]])),
    ("SELECT l.id AS id, s.name AS name FROM l LEFT JOIN r ON l.id = r.id LEFT JOIN s ON r.v = s.id WHERE s.name <> 'ten'", ok(['id', 'name'], [[2, 'twenty'], [3, 'thirty']])),
    ("SELECT l.id AS id, r.v AS v, s.name AS name FROM l LEFT JOIN r ON l.id = r.id JOIN s ON r.v = s.id WHERE r.v > 15", ok(['id', 'v', 'name'], [[2, 20, 'twenty'], [3, 30, 'thirty']])),
    ("SELECT l.id AS id, COUNT(*) AS n, COUNT(r.v) AS p FROM l LEFT JOIN r ON l.id = r.id GROUP BY l.id HAVING MAX(r.v) IS NULL", ok(['id', 'n', 'p'], [[4, 1, 0]])),
    ("SELECT DISTINCT r.v AS v, r.b AS b FROM l LEFT JOIN r ON l.id = r.id ORDER BY v NULLS FIRST, b DESC", ok(['v', 'b'], [[None, None], [10, True], [20, False], [30, None]])),
    ("SELECT l.id AS id FROM l LEFT JOIN r ON l.id = r.id WHERE 1 = 1 AND r.v = 10", ok(['id'], [[1]])),
    ("SELECT l.id AS id FROM l LEFT JOIN r ON l.id = r.id WHERE r.v = NULL", ok(['id'], [])),
    ("SELECT l.id AS id FROM l LEFT JOIN r ON NULL", ok(['id'], [[1], [2], [3], [4]])),
    ("SELECT l.id AS id FROM l LEFT JOIN r ON COUNT(*) > 0", error('INVALID_AGGREGATION')),
    ("SELECT l.id AS id FROM l LEFT JOIN r ON s.id = 1 JOIN s ON TRUE", error('UNKNOWN_COLUMN')),
]


def fixed():
    failures = 0
    for sql, expected in FIXED:
        for optimize in (False, True):
            if not expect(sql, expected, optimize): failures += 1
    return failures


# Randomized differential testing: optimized == unoptimized for generated queries.
COLS = {'l': ['id', 'k'], 'r': ['id', 'v', 'b'], 's': ['id', 'name']}
INT = {'l': ['l.id', 'l.k'], 'r': ['r.id', 'r.v'], 's': ['s.id']}


def rand_int(rng, tables):
    t = rng.choice(tables)
    c = rng.choice(INT[t])
    choice = rng.random()
    if choice < 0.5: return c
    if choice < 0.6: return 'NULL'
    if choice < 0.75: return str(rng.randint(-2, 30))
    if choice < 0.85: return f'COALESCE({rand_int(rng, tables)}, {rand_int(rng, tables)})'
    return f'({rand_int(rng, tables)} {rng.choice("+-*")} {rand_int(rng, tables)})'


def rand_bool(rng, tables, depth=0):
    choice = rng.random()
    if depth > 2 or choice < 0.4:
        a, b = rand_int(rng, tables), rand_int(rng, tables)
        return f'{a} {rng.choice(["=", "<>", "<", "<=", ">", ">="])} {b}'
    if choice < 0.5: return f'{rand_int(rng, tables)} IS {rng.choice(["", "NOT "])}NULL'
    if choice < 0.6 and 'r' in tables: return rng.choice(['r.b', 'NOT r.b', 'r.b IS NULL'])
    if choice < 0.65: return rng.choice(['TRUE', 'FALSE', 'NULL'])
    if choice < 0.8: return f'({rand_bool(rng, tables, depth + 1)} AND {rand_bool(rng, tables, depth + 1)})'
    if choice < 0.95: return f'({rand_bool(rng, tables, depth + 1)} OR {rand_bool(rng, tables, depth + 1)})'
    return f'NOT {rand_bool(rng, tables, depth + 1)}'


def rand_query(rng):
    order = ['l', 'r', 's']
    n = rng.randint(1, 3)
    tables, sql = [order[0]], 'FROM l'
    for t in order[1:n]:
        kind = rng.choice(['JOIN', 'LEFT JOIN', 'LEFT OUTER JOIN', 'INNER JOIN'])
        tables.append(t)
        on = rand_bool(rng, tables) if rng.random() < 0.3 else (f'l.id = r.id' if t == 'r' else 'r.v = s.id' if 'r' in tables else 'l.id = s.id')
        sql += f' {kind} {t} ON {on}'
    if rng.random() < 0.8: sql += f' WHERE {rand_bool(rng, tables)}'
    select = ', '.join(f'{t}.{c} AS {t}_{c}' for t in tables for c in COLS[t])
    sql = f'SELECT {select} {sql}'
    if rng.random() < 0.5:
        keys = [f'{t}_{c}' for t in tables for c in COLS[t]]
        rng.shuffle(keys)
        sql += ' ORDER BY ' + ', '.join(f'{k} {rng.choice(["", "ASC", "DESC"])} {rng.choice(["", "NULLS FIRST", "NULLS LAST"])}' for k in keys[:2])
    return sql


def differential(count):
    rng = random.Random(1234)
    failures = 0
    for _ in range(count):
        sql = rand_query(rng)
        a, b = run(DB, sql, False), run(DB, sql, True)
        if a != b:
            failures += 1
            print('DIFF', sql, '\n  plain', json.dumps(a), '\n  opt  ', json.dumps(b))
        elif not a['ok']:
            print('note: generated query errored', a['error']['code'], sql)
    return failures


if __name__ == '__main__':
    f = fixed() + differential(int(sys.argv[1]) if len(sys.argv) > 1 else 200)
    print('failures:', f)
    sys.exit(1 if f else 0)
