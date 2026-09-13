#!/usr/bin/env python3
"""Agent-authored checks for the query-null starter.

Complements the published corpus: every scenario is asserted against a
hand-computed expected response, and every successful query is additionally
required to produce identical output with the optimizer on and off.

Usage: python3 tests.py [path/to/run.sh]
"""
import json
import os
import subprocess
import sys

RUN = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "run.sh")


def col(name, kind, nullable):
    return {"name": name, "type": kind, "nullable": nullable}


DB = [
    {"name": "t",
     "columns": [col("id", "int", False), col("v", "int", True),
                 col("flag", "bool", True), col("label", "text", True)],
     "rows": [[1, 10, True, "oak"], [2, None, False, "elm"],
              [3, 10, None, None], [4, None, True, "oak"],
              [5, 5, False, None]]},
    {"name": "l",
     "columns": [col("id", "int", False)],
     "rows": [[1], [2], [3], [4]]},
    {"name": "r",
     "columns": [col("id", "int", True), col("v", "int", True)],
     "rows": [[1, 10], [1, None], [2, 20], [None, 99]]},
    {"name": "s",
     "columns": [col("id", "int", False), col("name", "text", False)],
     "rows": [[10, "ten"], [20, "twenty"]]},
]


def call(request):
    payload = request if isinstance(request, str) else json.dumps(request)
    proc = subprocess.run([RUN], input=payload, capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError("exit %d: %s" % (proc.returncode, proc.stderr[-400:]))
    return json.loads(proc.stdout)


def request(sql, optimize, database=None):
    return {"protocol_version": 1, "task": "query-null",
            "input": {"database": DB if database is None else database,
                      "queries": [{"sql": sql, "optimize": optimize}]}}


FAILURES = []


def check(label, got, want):
    if got != want:
        FAILURES.append("%s\n  want %s\n  got  %s"
                        % (label, json.dumps(want), json.dumps(got)))


def query(sql, columns, rows, database=None):
    """Both optimizer modes must return exactly these columns and rows."""
    want = {"ok": True, "result": {"results": [{"columns": columns, "rows": rows}]}}
    for optimize in (False, True):
        check("%s (optimize=%s)" % (sql, optimize),
              call(request(sql, optimize, database)), want)


def failure(sql, code, database=None):
    want = {"ok": False, "error": {"code": code}}
    for optimize in (False, True):
        check("%s (optimize=%s)" % (sql, optimize),
              call(request(sql, optimize, database)), want)


def rejects(label, payload, code):
    check(label, call(payload), {"ok": False, "error": {"code": code}})


# Three-valued expressions.
query("SELECT id AS id, v = v AS reflexive, NOT (v IS NULL) AS present FROM t",
      ["id", "reflexive", "present"],
      [[1, True, True], [2, None, False], [3, True, True],
       [4, None, False], [5, True, True]])
query("SELECT v = 10 IS NULL AS unknown, id AS id FROM t",
      ["unknown", "id"],
      [[False, 1], [True, 2], [False, 3], [True, 4], [False, 5]])
query("SELECT SUM(NULL) AS s, COUNT(NULL) AS c, MIN(NULL) AS m FROM t",
      ["s", "c", "m"], [[None, 0, None]])
query("SELECT COUNT(v + 1) AS n, COUNT(COALESCE(v, 0)) AS m, MIN(v + 1) AS lo FROM t",
      ["n", "m", "lo"], [[3, 5, 6]])
query("SELECT MIN(label) AS lo, MAX(label) AS hi FROM t WHERE label IS NULL",
      ["lo", "hi"], [[None, None]])
query("SELECT id AS id FROM t WHERE NULL", ["id"], [])

# NULL ordering.
query("SELECT id AS id, v AS v FROM t ORDER BY v ASC NULLS LAST, id DESC",
      ["id", "v"], [[5, 5], [3, 10], [1, 10], [4, None], [2, None]])
query("SELECT DISTINCT v AS v, flag AS f FROM t ORDER BY v NULLS FIRST, f NULLS FIRST",
      ["v", "f"],
      [[None, False], [None, True], [5, False], [10, None], [10, True]])

# Outer joins.
query("SELECT r.id AS rid, COUNT(*) AS n FROM l LEFT JOIN r ON l.id = r.id GROUP BY r.id",
      ["rid", "n"], [[1, 2], [2, 1], [None, 2]])
query("SELECT l.id AS id, r.v AS v FROM l LEFT JOIN r ON l.id = r.id ORDER BY v DESC, id ASC",
      ["id", "v"], [[1, None], [3, None], [4, None], [2, 20], [1, 10]])
query("SELECT COUNT(*) AS n FROM l LEFT JOIN r ON FALSE", ["n"], [[4]])
query("SELECT COUNT(*) AS n FROM l LEFT JOIN r ON NULL", ["n"], [[4]])
query("SELECT DISTINCT r.v AS v FROM l LEFT JOIN r ON l.id = r.id ORDER BY v NULLS FIRST",
      ["v"], [[None], [10], [20]])
# A padded right row must survive a WHERE that only IS NULL can satisfy.
query("SELECT a.id AS id, b.v AS bv FROM l AS a LEFT JOIN r AS b"
      " ON a.id = b.id WHERE a.id > 1 AND b.v IS NULL",
      ["id", "bv"], [[3, None], [4, None]])
# Mixed inner and outer joins: only the inner-joined source may be pre-filtered.
query("SELECT a.id AS id, b.v AS bv, c.name AS nm FROM l AS a JOIN r AS b"
      " ON a.id = b.id LEFT JOIN s AS c ON b.v = c.id WHERE a.id > 0 AND b.v > 5",
      ["id", "bv", "nm"], [[1, 10, "ten"], [2, 20, "twenty"]])

# Diagnostics.
failure("SELECT COALESCE(v) AS c FROM t", "PARSE_ERROR")
failure("SELECT v IS NOT 1 AS c FROM t", "PARSE_ERROR")
failure("SELECT id AS id FROM t ORDER BY id NULLS MIDDLE", "PARSE_ERROR")
failure("SELECT id AS id FROM l LEFT JOIN r", "PARSE_ERROR")
failure("SELECT count AS c FROM t", "PARSE_ERROR")
failure("SELECT id AS first FROM t", "PARSE_ERROR")
failure("SELECT id AS id FROM t WHERE id < 3 < 2", "PARSE_ERROR")
failure("SELECT MIN(flag) AS m FROM t", "TYPE_ERROR")
failure("SELECT COALESCE(flag, 1) AS c FROM t", "TYPE_ERROR")
failure("SELECT id AS id FROM t WHERE v", "TYPE_ERROR")
failure("SELECT SUM(COUNT(v)) AS c FROM t", "INVALID_AGGREGATION")
failure("SELECT id AS i FROM t GROUP BY id, id", "INVALID_AGGREGATION")
failure("SELECT id AS i FROM t HAVING id > 1", "INVALID_AGGREGATION")
failure("SELECT i AS i FROM t AS a JOIN t AS a ON TRUE", "DUPLICATE_ALIAS")

# Wire-level validation.
NON_NULLABLE = [{"name": "t", "columns": [col("id", "int", False)],
                 "rows": [[None]]}]
failure("SELECT id AS id FROM t", "INVALID_INPUT", NON_NULLABLE)
rejects("nullable must be a JSON boolean",
        request("SELECT id AS id FROM t", True,
                [{"name": "t",
                  "columns": [{"name": "id", "type": "int", "nullable": 1}],
                  "rows": [[1]]}]),
        "INVALID_INPUT")
rejects("unknown column field",
        request("SELECT id AS id FROM t", True,
                [{"name": "t",
                  "columns": [{"name": "id", "type": "int",
                               "nullable": False, "extra": 1}],
                  "rows": [[1]]}]),
        "INVALID_INPUT")
rejects("undecodable request", "{not json", "INVALID_INPUT")
check("empty database and query list",
      call({"protocol_version": 1, "task": "query-null",
            "input": {"database": [], "queries": []}}),
      {"ok": True, "result": {"results": []}})

if FAILURES:
    for item in FAILURES:
        print("FAIL " + item)
    print("%d failing check(s)" % len(FAILURES))
    sys.exit(1)
print("all checks passed")
