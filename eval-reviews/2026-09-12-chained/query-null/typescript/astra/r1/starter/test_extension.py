"""Additional contract regressions; run with python3 starter/test_extension.py."""
import json
import pathlib
import subprocess

RUN = pathlib.Path(__file__).with_name('run.sh')


def table(name, rows):
    return {'name': name, 'columns': [
        {'name': 'id', 'type': 'int', 'nullable': True}], 'rows': rows}


def check(database, sql, rows=None, error=None):
    queries = [{'sql': sql, 'optimize': mode} for mode in (False, True)]
    request = {'protocol_version': 1, 'task': 'query-null',
               'input': {'database': database, 'queries': queries}}
    result = subprocess.run([str(RUN.resolve())], input=json.dumps(request),
                            text=True, capture_output=True, check=True)
    actual = json.loads(result.stdout)
    if error:
        assert actual == {'ok': False, 'error': {'code': error}}, actual
    else:
        assert actual['ok'], actual
        for output in actual['result']['results']:
            assert output['rows'] == rows, (sql, output, rows)


db = [table('a', [[1], [2], [None]]), table('b', [[1], [1]]),
      table('c', [[2], [1], [None]])]
# Pushdown on a later inner source remains legal after an outer join.
check(db, 'SELECT a.id AS x, b.id AS y, c.id AS z FROM a '
      'LEFT JOIN b ON a.id = b.id JOIN c ON TRUE '
      'WHERE c.id = 2 AND a.id IS NOT NULL',
      [[1, 1, 2], [1, 1, 2], [2, None, 2]])
# A right-side predicate must not erase matches and manufacture padded rows.
check(db, 'SELECT a.id AS x FROM a LEFT JOIN b ON a.id = b.id '
      'WHERE COALESCE(b.id, 2) = 2', [[2], [None]])
check(db, 'SELECT COALESCE(NULL, NULL) AS x, MIN(NULL) AS y, '
      'COUNT(NULL) AS z, SUM(NULL) AS s FROM a', [[None, None, 0, None]])
check(db, 'SELECT NOT NULL IS NULL AS x, NULL IS NOT NULL OR TRUE AS y '
      'FROM a LIMIT 1', [[False, True]])
check(db, 'SELECT a.id AS x FROM a ORDER BY x DESC NULLS LAST',
      [[2], [1], [None]])
empty = [table('a', [])]
for sql, code in [
    ('SELECT COALESCE(1, TRUE) AS x FROM a', 'TYPE_ERROR'),
    ('SELECT NULL < TRUE AS x FROM a', 'TYPE_ERROR'),
    ('SELECT COALESCE(1, SUM(COUNT(*))) AS x FROM a', 'INVALID_AGGREGATION'),
    ('SELECT id IS NULL = TRUE AS x FROM a', 'PARSE_ERROR'),
    ('SELECT COALESCE(NULL) AS x FROM a', 'PARSE_ERROR'),
    ('SELECT id AS x FROM a WHERE NULL + 1', 'TYPE_ERROR'),
    ('SELECT id AS x FROM a GROUP BY id, a.id', 'INVALID_AGGREGATION'),
]:
    check(empty, sql, error=code)
print('12 additional regressions passed in both optimizer modes')
