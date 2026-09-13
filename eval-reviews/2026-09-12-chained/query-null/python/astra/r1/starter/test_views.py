"""Differential maintenance tests against checkpoint-one execution."""
import copy
import random
import unittest
from binder import bind
from engine import execute, optimize
from parser import Parser
from views import commands
from test_extension import table


class ViewTests(unittest.TestCase):
    def test_random_atomic_batches(self):
        sqls = [
            'SELECT k AS k, v AS v FROM aa',
            'SELECT DISTINCT v AS v FROM aa ORDER BY v DESC NULLS LAST LIMIT 3 OFFSET 1',
            'SELECT k AS k, COUNT(*) AS n, COUNT(v) AS c, SUM(v) AS s, MIN(v) AS lo, MAX(v) AS hi FROM aa GROUP BY k',
            'SELECT COUNT(v) AS c, SUM(v) AS s, MIN(v) AS lo, MAX(v) AS hi FROM aa HAVING COUNT(*) >= 0',
            'SELECT k + 1 AS k, COALESCE(SUM(v), 0) AS s FROM aa GROUP BY k HAVING SUM(v) > 0 ORDER BY s',
            'SELECT aa.k AS k, bb.v AS v FROM aa LEFT JOIN bb ON aa.k = bb.k WHERE aa.v > 0 AND bb.v IS NULL',
            'SELECT aa.k AS k, bb.v AS v FROM aa JOIN bb ON aa.k = bb.k WHERE aa.v > 0 AND bb.v > 0',
            'SELECT aa.k AS k, bb.v AS v, cc.v AS c FROM aa LEFT JOIN bb ON aa.k = bb.k LEFT JOIN cc ON bb.v = cc.k',
            'SELECT aa.k AS k, COUNT(bb.v) AS c, SUM(cc.v) AS s FROM aa LEFT JOIN bb ON aa.k = bb.k JOIN cc ON aa.k = cc.k GROUP BY aa.k',
            'SELECT a.k AS k, b.v AS v FROM aa AS a LEFT JOIN aa AS b ON a.k = b.k AND a.v < b.v',
            'SELECT aa.k AS k, bb.v AS v FROM aa LEFT JOIN bb ON aa.k < bb.k OR bb.v IS NULL',
            'SELECT aa.k AS k, bb.v AS v FROM aa LEFT JOIN bb ON aa.k = bb.k AND aa.v = bb.v',
        ]
        rng = random.Random(914)
        database = {n: table(n, [('k', 'int', True), ('v', 'int', True)],
                            [[rng.choice([None, 0, 1, 2]), rng.choice([None, -1, 0, 2])] for _ in range(4)])
                    for n in ('aa', 'bb', 'cc')}
        initial = copy.deepcopy(database)
        state = {n: dict(enumerate(t['rows'], 1)) for n, t in database.items()}
        next_id = 2000
        cmds, expected = [], []
        for enabled in (False, True):
            for i, sql in enumerate(sqls):
                cmds.append({'op': 'create', 'view': f'v{int(enabled)}-{i}', 'sql': sql, 'optimize': enabled})
                expected.append(None)
        for batch in range(70):
            changes = []
            for _ in range(rng.randint(1, 4)):
                n = rng.choice(list(state))
                action = rng.choice(['insert', 'update', 'delete']) if state[n] else 'insert'
                if action == 'insert':
                    rid = next_id
                    next_id -= 1  # Numeric ID order deliberately differs from encounter order.
                else: rid = rng.choice(list(state[n]))
                c = {'op': action, 'table': n, 'id': rid}
                if action == 'delete': del state[n][rid]
                else:
                    c['row'] = [rng.choice([None, 0, 1, 2]), rng.choice([None, -1, 0, 2])]
                    state[n][rid] = c['row']
                changes.append(c)
            cmds.append({'op': 'apply', 'changes': changes})
            expected.append(None)
            for n in state: database[n]['rows'] = list(state[n].values())
            for enabled in (False, True):
                for i, sql in enumerate(sqls):
                    plan = bind(Parser(sql).parse(), database)
                    result = execute(optimize(plan) if enabled else plan)
                    cmds.append({'op': 'read', 'view': f'v{int(enabled)}-{i}'})
                    expected.append(dict(result, revision=batch + 1))
        replies = commands(initial, cmds)
        for i, (reply, want) in enumerate(zip(replies, expected)):
            with self.subTest(command=i):
                self.assertTrue(reply['ok'], reply)
                if want is not None: self.assertEqual(reply['result'], want)

    def test_shape_prevalidation_and_id_rollback(self):
        db = {'aa': table('aa', [('k', 'int', False)], [[1]])}
        replies = commands(db, [
            {'op': 'apply', 'changes': [
                {'op': 'delete', 'table': 'missing', 'id': 1},
                {'op': 'insert', 'table': 'aa', 'id': True, 'row': [2]}]},
            {'op': 'apply', 'changes': [
                {'op': 'insert', 'table': 'aa', 'id': 2, 'row': [2]},
                {'op': 'update', 'table': 'aa', 'id': 1, 'row': [1000000001]}]},
            {'op': 'apply', 'changes': [{'op': 'insert', 'table': 'aa', 'id': 2, 'row': [3]}]},
            {'op': 'create', 'view': 'v', 'sql': 'SELECT k AS k FROM aa', 'optimize': True},
            {'op': 'read', 'view': 'v'},
            {'op': 'apply', 'changes': [{'op': 'update', 'table': 'aa', 'id': 1, 'row': [9]}]},
            {'op': 'read', 'view': 'v'},
        ])
        self.assertEqual(replies[0]['error']['code'], 'INVALID_COMMAND')
        self.assertEqual(replies[1]['error']['code'], 'INVALID_ROW')
        self.assertEqual(replies[2]['result']['revision'], 1)
        self.assertEqual(replies[4]['result']['rows'], [[1], [3]])
        self.assertEqual(replies[6]['result']['rows'], [[9], [3]])


if __name__ == '__main__': unittest.main()
