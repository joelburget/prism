"""Additional contract and optimizer regressions: python3 -m unittest discover -s starter."""
import unittest

from binder import bind
from engine import execute, optimize
from model import DomainError, validate
from parser import Parser


def table(name, specs, rows):
    return {'name': name, 'columns': [
        {'name': n, 'type': t, 'nullable': nullable} for n, t, nullable in specs
    ], 'rows': rows}


class ExtensionTests(unittest.TestCase):
    def setUp(self):
        self.tables = [
            table('aa', [('id', 'int', False)], [[1], [2], [3]]),
            table('bb', [('id', 'int', False), ('v', 'int', True)],
                  [[1, 0], [1, None], [2, 7]]),
            table('cc', [('id', 'int', False)], [[1], [1], [2], [3]]),
        ]

    def plan(self, sql, tables=None):
        database, _ = validate({'protocol_version': 1, 'task': 'query-null',
                                'input': {'database': self.tables if tables is None else tables,
                                          'queries': []}})
        return bind(Parser(sql).parse(), database)

    def rows(self, sql, expected, tables=None):
        for enabled in (False, True):
            with self.subTest(sql=sql, optimize=enabled):
                plan = self.plan(sql, tables)
                self.assertEqual(execute(optimize(plan) if enabled else plan)['rows'], expected)

    def error(self, sql, code):
        with self.subTest(sql=sql), self.assertRaisesRegex(DomainError, '^' + code + '$'):
            self.plan(sql)

    def test_mixed_join_filters_and_order(self):
        self.rows('SELECT aa.id AS a, bb.v AS b, cc.id AS c FROM aa '
                  'LEFT OUTER JOIN bb ON aa.id = bb.id '
                  'INNER JOIN cc ON aa.id = cc.id '
                  'WHERE aa.id > 0 AND bb.v IS NULL AND cc.id > 0',
                  [[1, None, 1], [1, None, 1], [3, None, 3]])
        self.rows('SELECT aa.id AS a FROM aa LEFT JOIN bb ON aa.id = bb.id '
                  'WHERE bb.v = 9', [])
        self.rows('SELECT aa.id AS a FROM aa LEFT JOIN bb ON NULL '
                  'WHERE bb.id IS NULL', [[1], [2], [3]])

    def test_empty_right_schema_padding(self):
        self.rows('SELECT aa.id AS a, bb.id AS b, bb.v AS c FROM aa '
                  'LEFT JOIN bb ON TRUE', [[1, None, None], [2, None, None], [3, None, None]],
                  [self.tables[0], table('bb', [('id', 'int', False), ('v', 'text', False)], [])])

    def test_optimizer_performs_safe_transformations(self):
        plan = optimize(self.plan(
            'SELECT COALESCE(NULL, 2 + 3) AS k, bb.id = bb.id AS same FROM aa '
            'LEFT JOIN bb ON aa.id = bb.id JOIN cc ON aa.id = cc.id '
            'WHERE aa.id > 0 AND bb.v IS NULL AND cc.id > 0'))
        self.assertEqual([len(fs) for fs in plan.filters], [1, 0, 1])
        self.assertEqual(plan.query.where.op, 'IS NULL')
        self.assertEqual(plan.query.select[0][0].op, 'lit')
        self.assertEqual(plan.query.select[0][0].value, 5)
        self.assertTrue(plan.query.select[1][0].nullable)
        inner = optimize(self.plan('SELECT aa.id AS a FROM aa JOIN bb ON aa.id = bb.id '
                                   'WHERE aa.id > 0 AND bb.v > 0'))
        self.assertEqual([len(fs) for fs in inner.filters], [1, 1])
        self.assertIsNone(inner.query.where)

    def test_contextual_null_types_and_unreachable_checks(self):
        self.rows('SELECT COALESCE(NULL, NULL) + 3 AS a, '
                  'NOT COALESCE(NULL, NULL) AS b, '
                  'COALESCE(MIN(NULL), MIN(NULL), \'x\') AS c FROM aa',
                  [[None, None, 'x']])
        for expr in ('COALESCE(1, TRUE)', 'COALESCE(TRUE, MIN(NULL))',
                     'FALSE AND COALESCE(NULL, 1)', 'NULL + \'x\'',
                     'NULL < TRUE', 'COALESCE(1, missing)', 'COALESCE(1, SUM(MAX(id)))'):
            code = ('UNKNOWN_COLUMN' if 'missing' in expr else
                    'INVALID_AGGREGATION' if 'SUM(MAX' in expr else 'TYPE_ERROR')
            self.error('SELECT ' + expr + ' AS a FROM aa WHERE FALSE', code)

    def test_aggregate_composition_and_empty_having(self):
        self.rows('SELECT COUNT(v) AS n, COUNT(NULL) AS z, SUM(v) AS s, '
                  'COALESCE(MIN(v), 9) AS m FROM bb WHERE FALSE HAVING COUNT(*) = 0',
                  [[0, 0, None, 9]])
        self.rows('SELECT COUNT(*) AS n FROM aa HAVING NULL', [])
        self.rows('SELECT v AS k, COUNT(v) AS n, SUM(v) AS s FROM bb GROUP BY v',
                  [[0, 1, 0], [None, 0, None], [7, 1, 7]])
        self.rows('SELECT COUNT(COALESCE(v, 0)) AS n, SUM(COALESCE(v, 2)) AS s FROM bb',
                  [[3, 9]])

    def test_stable_mixed_null_ordering(self):
        data = [table('data', [('id', 'int', False), ('a', 'bool', True),
                               ('b', 'text', True)],
                      [[1, None, 'x'], [2, False, None], [3, None, None],
                       [4, False, 'a'], [5, None, 'x'], [6, True, 'z']])]
        self.rows('SELECT id AS i, a AS x, b AS y FROM data '
                  'ORDER BY x DESC NULLS LAST, y ASC NULLS FIRST',
                  [[6, True, 'z'], [2, False, None], [4, False, 'a'],
                   [3, None, None], [1, None, 'x'], [5, None, 'x']], data)

    def test_parse_and_aggregation_errors(self):
        for sql in ('SELECT id = 1 IS NULL AS a FROM aa',
                    'SELECT id IS NULL = TRUE AS a FROM aa',
                    'SELECT COALESCE(NULL) AS a FROM aa',
                    'SELECT id AS a FROM aa ORDER BY a NULLS',
                    'SELECT id AS first FROM aa'):
            self.error(sql, 'PARSE_ERROR')
        self.error('SELECT id AS a FROM aa GROUP BY id, aa.id', 'INVALID_AGGREGATION')
        self.error('SELECT SUM(id) AS a FROM aa WHERE COUNT(*) > 0', 'INVALID_AGGREGATION')
        self.rows('SELECT NOT id IS NULL AS a, (id = 1) IS NULL AS b FROM aa',
                  [[True, False], [True, False], [True, False]])


if __name__ == '__main__':
    unittest.main()
