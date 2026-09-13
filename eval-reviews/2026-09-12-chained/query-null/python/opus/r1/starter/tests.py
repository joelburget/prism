#!/usr/bin/env python3
"""Agent-authored tests: edge cases, optimizer legality, and mode equivalence.

Run with `python3 tests.py` from this directory. The public corpus is run by the
bundle runner instead; these cases cover behavior the public cases do not pin.
"""
import json
import subprocess
import sys

from model import DomainError, validate
from parser import Parser
from binder import bind
from engine import execute, optimize
from session import Session

failures = []


def check(label, actual, expected):
    if actual != expected:
        failures.append(f'{label}: expected {expected!r}, got {actual!r}')


def run(database, sql, optimized):
    request = {'protocol_version': 1, 'task': 'query-null',
               'input': {'database': database, 'queries': [{'sql': sql, 'optimize': optimized}]}}
    try:
        db, _, _ = validate(request)
        plan = bind(Parser(sql).parse(), db)
        if optimized: plan = optimize(plan)
        return execute(plan), plan
    except DomainError as e:
        return str(e), None


def both(label, database, sql, expected):
    """Both optimizer modes must agree with the expected rows exactly."""
    for optimized in (False, True):
        result, _ = run(database, sql, optimized)
        rows = result['rows'] if isinstance(result, dict) else result
        check(f'{label} (optimize={optimized})', rows, expected)


def error(label, database, sql, code):
    for optimized in (False, True):
        result, _ = run(database, sql, optimized)
        check(f'{label} (optimize={optimized})', result, code)


def table(name, columns, rows):
    return {'name': name, 'rows': rows,
            'columns': [{'name': n, 'type': t, 'nullable': u} for n, t, u in columns]}


T = table('t', [('id', 'int', False), ('v', 'int', True), ('label', 'text', True), ('flag', 'bool', True)],
          [[1, 10, 'oak', True], [2, None, None, None], [3, 5, 'elm', False]])
L = table('l', [('id', 'int', False)], [[1], [2], [3]])
R = table('r', [('id', 'int', False), ('v', 'int', True)], [[1, 10], [1, None], [2, 20]])
DB = [T, L, R]

# --- three-valued logic and NULL-typed literals -----------------------------
both('null-literal-projection', DB, 'SELECT NULL AS n, id AS id FROM t LIMIT 1', [[None, 1]])
both('null-arith', DB, 'SELECT NULL + 1 AS a, 2 * NULL AS b FROM t LIMIT 1', [[None, None]])
both('coalesce-all-null', DB, 'SELECT COALESCE(NULL, NULL, NULL) AS c FROM t LIMIT 1', [[None]])
both('coalesce-chain', DB, 'SELECT id AS id, COALESCE(v, id, 0) AS c FROM t', [[1, 10], [2, 2], [3, 5]])
both('where-null-literal', DB, 'SELECT id AS id FROM t WHERE NULL', [])
both('not-unknown', DB, 'SELECT id AS id FROM t WHERE NOT flag', [[3]])
both('false-and-null', DB, 'SELECT id AS id, FALSE AND flag AS x FROM t LIMIT 1', [[1, False]])
both('is-null-on-text', DB, "SELECT id AS id FROM t WHERE label IS NOT NULL AND label <> 'oak'", [[3]])
both('parenthesized-is-null', DB, 'SELECT (v = 10) IS NULL AS x, id AS id FROM t',
     [[False, 1], [True, 2], [False, 3]])

# --- ordering, distinct, pagination ----------------------------------------
both('nulls-first-desc-explicit-last', DB, 'SELECT v AS v FROM t ORDER BY v DESC NULLS LAST',
     [[10], [5], [None]])
both('bool-order-nulls-last', DB, 'SELECT flag AS f FROM t ORDER BY f', [[False], [True], [None]])
both('distinct-null-row', DB, 'SELECT DISTINCT NULL AS n FROM t', [[None]])
both('offset-past-end', DB, 'SELECT id AS id FROM t ORDER BY id LIMIT 2 OFFSET 5', [])

# --- aggregation ------------------------------------------------------------
both('global-min-max-null-only', DB, 'SELECT MIN(v) AS lo, MAX(v) AS hi, SUM(v) AS s FROM t WHERE v IS NULL',
     [[None, None, None]])
both('global-sum-expression', DB, 'SELECT SUM(v) + COUNT(*) AS s FROM t', [[18]])
both('grouped-null-key-only', DB, 'SELECT label AS label, COUNT(*) AS n FROM t GROUP BY label',
     [['oak', 1], [None, 1], ['elm', 1]])
both('having-on-global-aggregate', DB, 'SELECT COUNT(*) AS n FROM t HAVING SUM(v) > 100', [])
both('group-by-no-aggregate', DB, 'SELECT v AS v FROM t GROUP BY v ORDER BY v', [[5], [10], [None]])

# --- joins ------------------------------------------------------------------
both('left-join-unmatched-padding', DB,
     'SELECT l.id AS id, r.v AS v FROM l LEFT JOIN r ON l.id = r.id',
     [[1, 10], [1, None], [2, 20], [3, None]])
both('left-join-on-always-false', DB, 'SELECT l.id AS id, r.id AS rid FROM l LEFT JOIN r ON FALSE',
     [[1, None], [2, None], [3, None]])
both('left-join-on-null', DB, 'SELECT l.id AS id, r.id AS rid FROM l LEFT JOIN r ON NULL',
     [[1, None], [2, None], [3, None]])
both('inner-join-empty-right', DB, 'SELECT l.id AS id FROM l JOIN r ON r.v = l.id', [])
both('left-then-left-where-left-only', DB,
     'SELECT l.id AS id, r.v AS v FROM l LEFT JOIN r ON l.id = r.id WHERE l.id > 1',
     [[2, 20], [3, None]])
both('left-join-where-is-null-keeps-padding', DB,
     'SELECT l.id AS id FROM l LEFT JOIN r ON l.id = r.id WHERE r.v IS NULL', [[1], [3]])
both('left-join-where-coalesce', DB,
     'SELECT l.id AS id FROM l LEFT JOIN r ON l.id = r.id WHERE COALESCE(r.v, 0) < 15',
     [[1], [1], [3]])

# --- diagnostics ------------------------------------------------------------
error('chained-is-null', DB, 'SELECT id = 1 IS NULL AS bad FROM t', 'PARSE_ERROR')
error('coalesce-one-argument', DB, 'SELECT COALESCE(v) AS c FROM t', 'PARSE_ERROR')
error('is-not-something', DB, 'SELECT v IS NOT 1 AS bad FROM t', 'PARSE_ERROR')
error('reserved-alias', DB, 'SELECT id AS first FROM t', 'PARSE_ERROR')
error('offset-without-limit', DB, 'SELECT id AS id FROM t OFFSET 1', 'PARSE_ERROR')
error('nulls-clause-typo', DB, 'SELECT id AS id FROM t ORDER BY id NULLS MIDDLE', 'PARSE_ERROR')
error('null-in-arithmetic-with-text', DB, "SELECT COALESCE(NULL, 1) + 'x' AS bad FROM t", 'TYPE_ERROR')
error('compare-across-types', DB, 'SELECT id AS id FROM t WHERE label = id', 'TYPE_ERROR')
error('bool-inequality', DB, 'SELECT id AS id FROM t WHERE flag < TRUE', 'TYPE_ERROR')
error('min-of-bool', DB, 'SELECT MIN(flag) AS m FROM t', 'TYPE_ERROR')
error('coalesce-bool-int', DB, 'SELECT COALESCE(flag, 1) AS c FROM t', 'TYPE_ERROR')
error('unknown-qualifier', DB, 'SELECT z.id AS id FROM t', 'UNKNOWN_COLUMN')
error('on-references-later-source', DB, 'SELECT l.id AS id FROM l LEFT JOIN r ON t.id = r.id JOIN t ON TRUE',
      'UNKNOWN_COLUMN')
error('duplicate-group-key', DB, 'SELECT id AS id, COUNT(*) AS n FROM t GROUP BY id, id',
      'INVALID_AGGREGATION')
error('aggregate-in-where', DB, 'SELECT id AS id FROM t WHERE COUNT(*) > 0', 'INVALID_AGGREGATION')
error('nested-aggregate', DB, 'SELECT SUM(SUM(v)) AS s FROM t', 'INVALID_AGGREGATION')
error('having-without-aggregate', DB, 'SELECT id AS id FROM t HAVING id > 1', 'INVALID_AGGREGATION')
error('duplicate-source-alias', DB, 'SELECT x.id AS id FROM t AS x JOIN t AS x ON TRUE', 'DUPLICATE_ALIAS')
error('unknown-table', DB, 'SELECT id AS id FROM nope', 'UNKNOWN_TABLE')


def invalid(label, database):
    request = {'protocol_version': 1, 'task': 'query-null', 'input': {'database': database, 'queries': []}}
    try:
        validate(request)
        check(label, 'ok', 'INVALID_INPUT')
    except DomainError as e:
        check(label, str(e), 'INVALID_INPUT')


invalid('null-in-required-column', [table('x', [('id', 'int', False)], [[None]])])
invalid('reserved-table-name', [table('count', [('id', 'int', False)], [])])
invalid('bool-for-int-column', [table('x', [('id', 'int', False)], [[True]])])
invalid('float-value', [table('x', [('id', 'int', True)], [[1.5]])])
invalid('nullable-not-boolean', [{'name': 'x', 'rows': [],
                                  'columns': [{'name': 'id', 'type': 'int', 'nullable': 1}]}])
invalid('unknown-column-field', [{'name': 'x', 'rows': [],
                                  'columns': [{'name': 'id', 'type': 'int', 'nullable': True, 'pk': True}]}])

# --- the optimizer must actually transform plans ---------------------------
_, plan = run(DB, 'SELECT l.id AS id, r.v AS v FROM l JOIN r ON l.id = r.id WHERE l.id > 1 AND r.v > 5', True)
check('inner-join pushes both single-source filters', [len(f) for f in plan.filters], [1, 1])
check('inner-join leaves no residual WHERE', plan.query.where, None)

_, plan = run(DB, 'SELECT l.id AS id FROM l LEFT JOIN r ON l.id = r.id WHERE r.v > 5', True)
check('null-rejecting filter is pushed', [len(f) for f in plan.filters], [0, 1])
check('null-rejecting filter turns the join inner', plan.query.joins[0].outer, False)

_, plan = run(DB, 'SELECT l.id AS id FROM l LEFT JOIN r ON l.id = r.id WHERE r.v IS NULL', True)
check('null-accepting filter stays above the join', [len(f) for f in plan.filters], [0, 0])
check('null-accepting filter keeps the outer join', plan.query.joins[0].outer, True)

_, plan = run(DB, 'SELECT 1 + 2 * 3 AS c, id AS id FROM t', True)
check('arithmetic is folded', plan.query.select[0][0].value, 7)

_, plan = run(DB, 'SELECT id AS id FROM t WHERE id = id AND id > 0', True)
check('non-nullable self-equality folds away', len(plan.filters[0]) + (plan.query.where is not None), 1)

_, plan = run(DB, 'SELECT id AS id FROM t WHERE v = v', True)
check('nullable self-equality is preserved', plan.query.where.op, '=')

_, plan = run(DB, 'SELECT id AS id FROM t WHERE flag OR NOT flag', True)
check('nullable excluded middle is preserved', plan.query.where.op, 'OR')

_, plan = run(DB, 'SELECT id AS id FROM t AS a WHERE a.id IS NOT NULL', True)
check('non-nullable IS NOT NULL folds to TRUE', plan.query.where.value, True)

# --- checkpoint two: incrementally maintained views -------------------------

def session(database, commands):
    """One command-mode request, returned as the list of replies."""
    request = {'protocol_version': 1, 'task': 'query-null',
               'input': {'database': database, 'commands': commands}}
    try:
        db, mode, items = validate(request)
        check('command mode selected', mode, 'commands')
        return Session(db).run(items)
    except DomainError as e:
        return str(e)


def codes(replies):
    return [r['error']['code'] if not r['ok'] else r['result'] for r in replies]


L = table('l', [('k', 'int', True), ('v', 'int', True)], [[1, 10], [2, 20], [None, 30]])
R = table('r', [('k', 'int', True), ('w', 'int', True)], [[1, 7], [1, 8], [None, 9]])
JOIN = 'SELECT l.k AS k, r.w AS w FROM l LEFT JOIN r ON l.k = r.k ORDER BY k NULLS LAST, w NULLS LAST'

# Both optimizer settings must maintain the same view.
for optimized in (False, True):
    replies = session([L, R], [
        {'op': 'create', 'view': 'v', 'sql': JOIN, 'optimize': optimized},
        {'op': 'apply', 'changes': [{'op': 'update', 'table': 'l', 'id': 3, 'row': [1, 30]}]},
        {'op': 'read', 'view': 'v'}])
    check(f'update into a join (optimize={optimized})', replies[2]['result']['rows'],
          [[1, 7], [1, 7], [1, 8], [1, 8], [2, None]])

# A batch that ends where it started still advances the revision.
replies = session([L, R], [
    {'op': 'create', 'view': 'v', 'sql': JOIN, 'optimize': False},
    {'op': 'apply', 'changes': [{'op': 'delete', 'table': 'l', 'id': 1},
                                {'op': 'insert', 'table': 'l', 'id': 4, 'row': [1, 10]}]},
    {'op': 'read', 'view': 'v'}])
check('no-op batch advances the revision', replies[2]['result']['revision'], 1)
check('reinserted row keeps its content', replies[2]['result']['rows'], [[1, 7], [1, 8], [2, None], [None, None]])

# An insert followed by a delete in one batch still burns the identifier.
replies = session([L, R], [
    {'op': 'apply', 'changes': [{'op': 'insert', 'table': 'l', 'id': 9, 'row': [5, 1]},
                                {'op': 'delete', 'table': 'l', 'id': 9}]},
    {'op': 'apply', 'changes': [{'op': 'insert', 'table': 'l', 'id': 9, 'row': [5, 1]}]}])
check('insert-then-delete reserves the id', codes(replies), [{'revision': 1}, 'ROW_ID_USED'])

# A failed batch must not consume identifiers or leave view state behind.
replies = session([L, R], [
    {'op': 'create', 'view': 'v', 'sql': 'SELECT COUNT(*) AS n, SUM(v) AS s FROM l', 'optimize': True},
    {'op': 'apply', 'changes': [{'op': 'insert', 'table': 'l', 'id': 4, 'row': [7, 1]},
                                {'op': 'update', 'table': 'l', 'id': 8, 'row': [7, 1]}]},
    {'op': 'read', 'view': 'v'},
    {'op': 'apply', 'changes': [{'op': 'insert', 'table': 'l', 'id': 4, 'row': [7, 1]}]},
    {'op': 'read', 'view': 'v'}])
check('rolled back batch', codes(replies)[1], 'UNKNOWN_ROW')
check('rollback leaves the view intact', replies[2]['result'], {'revision': 0, 'columns': ['n', 's'], 'rows': [[3, 60]]})
check('rollback frees the id', replies[4]['result'], {'revision': 1, 'columns': ['n', 's'], 'rows': [[4, 61]]})

# Dropping a view releases its state; the name may be rebound to other SQL.
replies = session([L, R], [
    {'op': 'create', 'view': 'v', 'sql': JOIN, 'optimize': False},
    {'op': 'drop', 'view': 'v'},
    {'op': 'drop', 'view': 'v'},
    {'op': 'read', 'view': 'v'},
    {'op': 'create', 'view': 'v', 'sql': 'SELECT k AS k FROM l ORDER BY k NULLS LAST', 'optimize': False},
    {'op': 'read', 'view': 'v'}])
check('drop lifecycle', codes(replies)[1:4], [{'dropped': 'v'}, 'UNKNOWN_VIEW', 'UNKNOWN_VIEW'])
check('rebound view', replies[5]['result']['rows'], [[1], [2], [None]])

# Creation reports the earlier errors of the query compiler.
for sql, code in [('SELECT k FROM l', 'PARSE_ERROR'), ('SELECT k AS k FROM nope', 'UNKNOWN_TABLE'),
                  ('SELECT k AS k FROM l JOIN r ON l.k = r.k', 'AMBIGUOUS_COLUMN'),
                  ('SELECT v + s AS x FROM l', 'UNKNOWN_COLUMN'),
                  ('SELECT k AS k, v AS k FROM l', 'DUPLICATE_ALIAS'),
                  ('SELECT SUM(k) AS s FROM l GROUP BY k HAVING SUM(SUM(k)) > 1', 'INVALID_AGGREGATION'),
                  ('SELECT k AS k FROM l WHERE v', 'TYPE_ERROR')]:
    replies = session([L, R], [{'op': 'create', 'view': 'v', 'sql': sql, 'optimize': True}])
    check(f'create reports {code}', codes(replies), [code])

replies = session([L, R], [{'op': 'create', 'view': 'v', 'sql': JOIN, 'optimize': False},
                           {'op': 'create', 'view': 'v', 'sql': JOIN, 'optimize': False}])
check('duplicate view', codes(replies)[1], 'VIEW_EXISTS')

# Command and change shapes.
for command, code in [
        ({'op': 'nope'}, 'INVALID_COMMAND'),
        ('create', 'INVALID_COMMAND'),
        ({'op': 'create', 'view': 'v', 'sql': JOIN}, 'INVALID_COMMAND'),
        ({'op': 'create', 'view': 'v', 'sql': JOIN, 'optimize': 1}, 'INVALID_COMMAND'),
        ({'op': 'create', 'view': 'V', 'sql': JOIN, 'optimize': True}, 'INVALID_COMMAND'),
        ({'op': 'create', 'view': 'a_b', 'sql': JOIN, 'optimize': True}, 'INVALID_COMMAND'),
        ({'op': 'create', 'view': 'a' * 41, 'sql': JOIN, 'optimize': True}, 'INVALID_COMMAND'),
        ({'op': 'read', 'view': 'v', 'extra': 1}, 'INVALID_COMMAND'),
        ({'op': 'apply', 'changes': []}, 'INVALID_COMMAND'),
        ({'op': 'apply', 'changes': [{'op': 'delete', 'table': 'l', 'id': 1}] * 201}, 'INVALID_COMMAND'),
        ({'op': 'apply', 'changes': {}}, 'INVALID_COMMAND'),
        ({'op': 'apply', 'changes': [{'op': 'delete', 'table': 'l', 'id': True}]}, 'INVALID_COMMAND'),
        ({'op': 'apply', 'changes': [{'op': 'delete', 'table': 'l', 'id': 0}]}, 'INVALID_COMMAND'),
        ({'op': 'apply', 'changes': [{'op': 'delete', 'table': 'L', 'id': 1}]}, 'INVALID_COMMAND'),
        ({'op': 'apply', 'changes': [{'op': 'insert', 'table': 'l', 'id': 5, 'row': {}}]}, 'INVALID_COMMAND'),
        ({'op': 'apply', 'changes': [{'op': 'insert', 'table': 'l', 'id': 5}]}, 'INVALID_COMMAND'),
        ({'op': 'apply', 'changes': [{'op': 'delete', 'table': 'select', 'id': 1}]}, 'UNKNOWN_TABLE'),
        ({'op': 'apply', 'changes': [{'op': 'delete', 'table': 'gone', 'id': 1}]}, 'UNKNOWN_TABLE'),
        ({'op': 'apply', 'changes': [{'op': 'insert', 'table': 'l', 'id': 5, 'row': [1]}]}, 'INVALID_ROW'),
        ({'op': 'apply', 'changes': [{'op': 'insert', 'table': 'l', 'id': 5, 'row': [True, 1]}]}, 'INVALID_ROW'),
        ({'op': 'apply', 'changes': [{'op': 'insert', 'table': 'l', 'id': 5, 'row': [1, 2000000000]}]}, 'INVALID_ROW'),
        ({'op': 'apply', 'changes': [{'op': 'update', 'table': 'l', 'id': 1, 'row': [1, 'x']}]}, 'INVALID_ROW'),
        ({'op': 'apply', 'changes': [{'op': 'insert', 'table': 'l', 'id': 1, 'row': [1, 2]}]}, 'ROW_ID_USED'),
        ({'op': 'apply', 'changes': [{'op': 'update', 'table': 'l', 'id': 7, 'row': [1, 2]}]}, 'UNKNOWN_ROW')]:
    check(f'shape {code} for {command}', codes(session([L, R], [command])), [code])

NN = table('n', [('id', 'int', False)], [[1]])
check('null into a non-nullable column',
      codes(session([NN], [{'op': 'apply', 'changes': [{'op': 'update', 'table': 'n', 'id': 1, 'row': [None]}]}])),
      ['INVALID_ROW'])

# The two input forms are exclusive, and neither may be missing.
for payload in [{'database': [], 'queries': [], 'commands': []}, {'database': []},
                {'database': [], 'commands': {}}, {'commands': []}]:
    request = {'protocol_version': 1, 'task': 'query-null', 'input': payload}
    try:
        validate(request)
        check(f'rejects {payload}', 'accepted', 'INVALID_INPUT')
    except DomainError as e:
        check(f'rejects {payload}', str(e), 'INVALID_INPUT')

check('empty command list', session([], []), [])

# Incremental maintenance must agree with a fresh batch query at every step.
def batch(database, sql, optimized):
    plan = bind(Parser(sql).parse(), {t['name']: t for t in database})
    return execute(optimize(plan) if optimized else plan)['rows']


SQL = [JOIN,
       'SELECT k AS k, COUNT(*) AS n, COUNT(v) AS c, SUM(v) AS s, MIN(v) AS lo, MAX(v) AS hi FROM l'
       ' GROUP BY k ORDER BY k NULLS LAST',
       'SELECT DISTINCT COALESCE(v, 0) AS c FROM l ORDER BY c DESC LIMIT 2 OFFSET 1',
       'SELECT l.k AS k, r.w AS w FROM l LEFT JOIN r ON l.k = r.k WHERE r.w > 7 ORDER BY k, w',
       'SELECT COUNT(*) AS n, MIN(v) AS lo FROM l WHERE k IS NULL']
SCRIPT = [[{'op': 'delete', 'table': 'r', 'id': 1}],
          [{'op': 'update', 'table': 'l', 'id': 2, 'row': [1, 21]}],
          [{'op': 'insert', 'table': 'r', 'id': 4, 'row': [2, 4]}, {'op': 'delete', 'table': 'l', 'id': 1}],
          [{'op': 'delete', 'table': 'r', 'id': 2}, {'op': 'delete', 'table': 'r', 'id': 3},
           {'op': 'delete', 'table': 'r', 'id': 4}],
          [{'op': 'update', 'table': 'l', 'id': 3, 'row': [None, None]}],
          [{'op': 'delete', 'table': 'l', 'id': 2}, {'op': 'delete', 'table': 'l', 'id': 3}]]
for optimized in (False, True):
    commands = [{'op': 'create', 'view': 'v%d' % i, 'sql': sql, 'optimize': optimized}
                for i, sql in enumerate(SQL)]
    for changes in SCRIPT:
        commands.append({'op': 'apply', 'changes': changes})
        commands.extend({'op': 'read', 'view': 'v%d' % i} for i in range(len(SQL)))
    replies = session([L, R], commands)
    rows = {'l': [list(r) for r in L['rows']], 'r': [list(r) for r in R['rows']]}
    ids = {'l': [1, 2, 3], 'r': [1, 2, 3]}
    at = len(SQL)
    for changes in SCRIPT:
        for change in changes:  # replay the same edits against a plain table copy
            t = change['table']
            if change['op'] == 'delete':
                rows[t].pop(ids[t].index(change['id']))
                ids[t].pop(ids[t].index(change['id']))
            elif change['op'] == 'update':
                rows[t][ids[t].index(change['id'])] = change['row']
            else:
                rows[t].append(change['row'])
                ids[t].append(change['id'])
        database = [table('l', [('k', 'int', True), ('v', 'int', True)], rows['l']),
                    table('r', [('k', 'int', True), ('w', 'int', True)], rows['r'])]
        at += 1
        for i, sql in enumerate(SQL):
            check(f'view {i} matches a fresh query (optimize={optimized}, after {changes})',
                  replies[at + i]['result']['rows'], batch(database, sql, optimized))
        at += len(SQL)

# --- end-to-end wire protocol ----------------------------------------------
request = {'protocol_version': 1, 'task': 'query-null',
           'input': {'database': [T], 'queries': [{'sql': 'SELECT COUNT(*) AS n FROM t', 'optimize': True}]}}
process = subprocess.run([sys.executable, 'main.py'], input=json.dumps(request),
                         capture_output=True, text=True)
check('subprocess exit status', process.returncode, 0)
check('subprocess response', json.loads(process.stdout),
      {'ok': True, 'result': {'results': [{'columns': ['n'], 'rows': [[3]]}]}})
request = {'protocol_version': 1, 'task': 'query-null',
           'input': {'database': [T], 'commands': [
               {'op': 'create', 'view': 'live', 'sql': 'SELECT COUNT(*) AS n FROM t', 'optimize': False},
               {'op': 'apply', 'changes': [{'op': 'delete', 'table': 't', 'id': 1}]},
               {'op': 'read', 'view': 'live'}]}}
process = subprocess.run([sys.executable, 'main.py'], input=json.dumps(request),
                         capture_output=True, text=True)
check('command-mode response', json.loads(process.stdout), {'ok': True, 'result': {'results': [
    {'ok': True, 'result': {'view': 'live', 'revision': 0}},
    {'ok': True, 'result': {'revision': 1}},
    {'ok': True, 'result': {'revision': 1, 'columns': ['n'], 'rows': [[2]]}}]}})

process = subprocess.run([sys.executable, 'main.py'], input='not json', capture_output=True, text=True)
check('malformed request exits zero', process.returncode, 0)
check('malformed request response', json.loads(process.stdout),
      {'ok': False, 'error': {'code': 'INVALID_INPUT'}})

for f in failures: print('FAIL', f)
print(f'{"FAILED" if failures else "ok"}: {len(failures)} failures')
sys.exit(1 if failures else 0)
