"""Additional contract checks; run after build.sh with python3 starter/test_extension.py."""
import json
from pathlib import Path
import subprocess

RUN = str(Path(__file__).resolve().with_name('run.sh'))
DB = [
    {'name': 'items', 'columns': [
        {'name': 'id', 'type': 'int', 'nullable': False},
        {'name': 'v', 'type': 'int', 'nullable': True},
    ], 'rows': [[1, None], [2, 3], [3, None]]},
    {'name': 'empty', 'columns': [
        {'name': 'id', 'type': 'int', 'nullable': False},
        {'name': 'label', 'type': 'text', 'nullable': False},
    ], 'rows': []},
]


def check(sql, columns=None, rows=None, error=None, database=DB):
    for optimize in (False, True):
        request = {'protocol_version': 1, 'task': 'query-null', 'input': {
            'database': database, 'queries': [{'sql': sql, 'optimize': optimize}]}}
        proc = subprocess.run([RUN], input=json.dumps(request) + '\n',
                              text=True, capture_output=True, timeout=10, check=True)
        actual = json.loads(proc.stdout)
        expected = ({'ok': False, 'error': {'code': error}} if error else
                    {'ok': True, 'result': {'results': [{'columns': columns, 'rows': rows}]}})
        assert actual == expected, (sql, optimize, actual, expected)


check('SELECT i.id AS id, e.id AS other, e.label AS label FROM items AS i '
      'LEFT OUTER JOIN empty AS e ON TRUE',
      ['id', 'other', 'label'], [[1, None, None], [2, None, None], [3, None, None]])
check('SELECT COUNT(*) AS n, COUNT(e.id) AS matched, SUM(e.id) AS total '
      'FROM items AS i LEFT JOIN empty AS e ON NULL',
      ['n', 'matched', 'total'], [[3, 0, None]])
check('SELECT i.id AS id FROM items AS i LEFT JOIN items AS j ON i.id = j.id '
      'WHERE j.v IS NULL', ['id'], [[1], [3]])
check('SELECT i.id AS id FROM items AS i JOIN items AS j ON i.id = j.id '
      'WHERE j.v IS NULL AND i.id > 1', ['id'], [[3]])
check('SELECT COALESCE(NULL, NULL) AS n, NOT NULL AS b, NULL IS NOT NULL AS c '
      'FROM items LIMIT 1', ['n', 'b', 'c'], [[None, None, False]])
check('SELECT COALESCE(NULL, NULL) + 4 AS n, NULL < NULL AS b FROM items LIMIT 1',
      ['n', 'b'], [[None, None]])
check('SELECT COUNT(NULL) AS n, MIN(NULL) AS lo, MAX(NULL) AS hi, SUM(NULL) AS s '
      'FROM empty', ['n', 'lo', 'hi', 's'], [[0, None, None, None]])
check('SELECT COUNT(*) AS n FROM empty HAVING NULL', ['n'], [])
check('SELECT v AS v, id AS id FROM items ORDER BY v DESC NULLS LAST, id DESC',
      ['v', 'id'], [[3, 2], [None, 3], [None, 1]])
check('SELECT v AS v, COUNT(*) AS n FROM items GROUP BY v HAVING v IS NULL',
      ['v', 'n'], [[None, 2]])
for sql in [
    'SELECT COALESCE(1, FALSE) AS x FROM empty',
    'SELECT COALESCE(1, NULL + TRUE) AS x FROM empty',
    'SELECT NULL < TRUE AS x FROM empty',
    'SELECT MIN(FALSE) AS x FROM empty',
    'SELECT id AS x FROM empty WHERE NULL OR 1',
]:
    check(sql, error='TYPE_ERROR')
for sql in [
    'SELECT COALESCE(NULL) AS x FROM items',
    'SELECT id IS NULL IS NULL AS x FROM items',
    'SELECT id AS x FROM items ORDER BY x NULLS',
    'SELECT id AS first FROM items',
]:
    check(sql, error='PARSE_ERROR')
for sql in [
    'SELECT COALESCE(SUM(COUNT(*)), 0) AS x FROM empty',
    'SELECT id AS x, SUM(NULL) AS y FROM empty',
    'SELECT id AS x FROM empty GROUP BY id, empty.id',
    'SELECT id AS x FROM empty WHERE COUNT(*) > 0',
]:
    check(sql, error='INVALID_AGGREGATION')
check('SELECT COALESCE(1, missing) AS x FROM empty', error='UNKNOWN_COLUMN')
check('SELECT empty.id AS x FROM empty AS e', error='UNKNOWN_COLUMN')
check('SELECT id AS x FROM items', error='INVALID_INPUT', database=[{
    'name': 'items', 'columns': [{'name': 'id', 'type': 'int', 'nullable': False}],
    'rows': [[None]],
}])
print('26 additional scenarios passed in both optimizer modes')
