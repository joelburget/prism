#!/usr/bin/env python3
"""Differential checks for incrementally maintained views.

Every read of a maintained view is compared against the answer the one-shot
query engine gives for the same SQL over the view's current base rows, which
is the contract's definition of what a view must contain. Scenarios are drawn
randomly from join, grouping, ordering and pagination shapes, and each is run
with the optimizer both on and off.

Usage: python3 tests_incremental.py [path/to/run.sh] [iterations]
"""
import json
import os
import random
import subprocess
import sys

RUN = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "run.sh")
ITERATIONS = int(sys.argv[2]) if len(sys.argv) > 2 else 60

FAILURES = []


def call(request):
    payload = request if isinstance(request, str) else json.dumps(request)
    proc = subprocess.run([RUN], input=payload, capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError("exit %d: %s" % (proc.returncode, proc.stderr[-400:]))
    return json.loads(proc.stdout)


def check(label, got, want):
    if got != want:
        FAILURES.append("%s\n  want %s\n  got  %s"
                        % (label, json.dumps(want), json.dumps(got)))


def col(name, kind, nullable):
    return {"name": name, "type": kind, "nullable": nullable}


def value(kind, nullable, rng):
    if nullable and rng.random() < 0.3:
        return None
    if kind == "int":
        return rng.choice([0, 1, 2, 3, -1, 10])
    if kind == "bool":
        return rng.random() < 0.5
    return rng.choice(["a", "b", "c"])


SCHEMAS = [
    ("l", [col("k", "int", True), col("v", "int", True), col("w", "text", True)]),
    ("r", [col("k", "int", True), col("m", "int", True)]),
]

QUERIES = [
    "SELECT k AS k, v AS v FROM l ORDER BY k NULLS LAST, v NULLS LAST",
    "SELECT DISTINCT k AS k FROM l ORDER BY k NULLS FIRST",
    "SELECT k AS k, COUNT(*) AS n, COUNT(v) AS nv, SUM(v) AS s, MIN(v) AS lo,"
    " MAX(v) AS hi FROM l GROUP BY k ORDER BY k NULLS LAST",
    "SELECT COUNT(*) AS n, SUM(v) AS s, MIN(w) AS lo, MAX(w) AS hi FROM l",
    "SELECT k AS k, SUM(v) AS s FROM l GROUP BY k HAVING SUM(v) > 1"
    " ORDER BY s DESC NULLS LAST, k NULLS LAST",
    "SELECT l.k AS k, l.v AS v, r.m AS m FROM l LEFT JOIN r ON l.k = r.k"
    " ORDER BY k NULLS LAST, v NULLS LAST, m NULLS LAST",
    "SELECT l.k AS k, r.m AS m FROM l JOIN r ON l.k = r.k WHERE l.v > 0"
    " ORDER BY k NULLS LAST, m NULLS LAST",
    "SELECT l.k AS k, r.m AS m FROM l LEFT JOIN r ON l.k = r.k AND r.m > 0"
    " WHERE l.v IS NULL OR l.v > 0 ORDER BY k NULLS LAST, m NULLS LAST",
    "SELECT a.k AS ak, b.k AS bk FROM l AS a LEFT JOIN l AS b ON a.v = b.v"
    " ORDER BY ak NULLS LAST, bk NULLS LAST",
    "SELECT l.k AS k, COUNT(r.m) AS n, MAX(r.m) AS hi FROM l LEFT JOIN r"
    " ON l.k = r.k GROUP BY l.k ORDER BY k NULLS LAST",
    "SELECT v AS v FROM l ORDER BY v DESC NULLS LAST LIMIT 3 OFFSET 1",
    "SELECT l.k AS k, r.m AS m FROM l LEFT JOIN r ON l.k = r.k"
    " LEFT JOIN r AS c ON r.m = c.m ORDER BY k NULLS LAST, m NULLS LAST",
]


class Mirror:
    """The live rows of each table, in encounter order, as the contract says."""

    def __init__(self, database):
        self.tables = {}
        for table in database:
            rows = [(i + 1, list(row)) for i, row in enumerate(table["rows"])]
            self.tables[table["name"]] = {
                "columns": table["columns"],
                "rows": rows,
                "used": set(i + 1 for i in range(len(table["rows"]))),
            }

    def apply(self, changes):
        """Returns None when the batch is legal, else the expected error."""
        state = {n: {"rows": list(t["rows"]), "used": set(t["used"])}
                 for n, t in self.tables.items()}
        for change in changes:
            table = state[change["table"]]
            ids = [i for i, _ in table["rows"]]
            if change["op"] == "insert":
                if change["id"] in table["used"]:
                    return "ROW_ID_USED"
                table["used"].add(change["id"])
                table["rows"].append((change["id"], change["row"]))
            elif change["op"] == "update":
                if change["id"] not in ids:
                    return "UNKNOWN_ROW"
                at = ids.index(change["id"])
                table["rows"][at] = (change["id"], change["row"])
            else:
                if change["id"] not in ids:
                    return "UNKNOWN_ROW"
                del table["rows"][ids.index(change["id"])]
        for name, table in state.items():
            self.tables[name]["rows"] = table["rows"]
            self.tables[name]["used"] = table["used"]
        return None

    def database(self):
        return [{"name": name, "columns": t["columns"],
                 "rows": [row for _, row in t["rows"]]}
                for name, t in self.tables.items()]


def oracle(database, sql, optimize):
    reply = call({"protocol_version": 1, "task": "query-null",
                  "input": {"database": database,
                            "queries": [{"sql": sql, "optimize": optimize}]}})
    assert reply["ok"], reply
    return reply["result"]["results"][0]


def scenario(rng, sql, optimize):
    database = []
    for name, columns in SCHEMAS:
        rows = [[value(c["type"], c["nullable"], rng) for c in columns]
                for _ in range(rng.randrange(0, 5))]
        database.append({"name": name, "columns": columns, "rows": rows})

    mirror = Mirror(json.loads(json.dumps(database)))
    next_id = {name: 100 for name, _ in SCHEMAS}
    revision = 0
    commands = [{"op": "create", "view": "v", "sql": sql, "optimize": optimize}]
    expect = [{"ok": True, "result": {"view": "v", "revision": 0}}]
    snapshots = [None]

    def read():
        commands.append({"op": "read", "view": "v"})
        expect.append(None)
        snapshots.append((len(expect) - 1, revision, mirror.database()))

    read()
    for _ in range(rng.randrange(1, 6)):
        changes = []
        for _ in range(rng.randrange(1, 4)):
            name, columns = SCHEMAS[rng.randrange(len(SCHEMAS))]
            live = [i for i, _ in mirror.tables[name]["rows"]]
            pick = rng.random()
            if pick < 0.4 or not live:
                next_id[name] += 1
                changes.append({"op": "insert", "table": name,
                                "id": next_id[name],
                                "row": [value(c["type"], c["nullable"], rng)
                                        for c in columns]})
            elif pick < 0.7:
                changes.append({"op": "update", "table": name,
                                "id": rng.choice(live),
                                "row": [value(c["type"], c["nullable"], rng)
                                        for c in columns]})
            else:
                changes.append({"op": "delete", "table": name,
                                "id": rng.choice(live)})
        commands.append({"op": "apply", "changes": changes})
        error = mirror.apply(changes)
        if error is None:
            revision += 1
            expect.append({"ok": True, "result": {"revision": revision}})
        else:
            expect.append({"ok": False, "error": {"code": error}})
        read()

    reply = call({"protocol_version": 1, "task": "query-null",
                  "input": {"database": database, "commands": commands}})
    assert reply["ok"], reply
    results = reply["result"]["results"]
    check("reply count", len(results), len(commands))
    for at, revision_at, snapshot in snapshots[1:]:
        want = oracle(snapshot, sql, optimize)
        expect[at] = {"ok": True, "result": {"revision": revision_at,
                                             "columns": want["columns"],
                                             "rows": want["rows"]}}
    for command, want, got in zip(commands, expect, results):
        check("%s\n  command %s" % (sql, json.dumps(command)), got, want)


def main():
    rng = random.Random(20260913)
    for i in range(ITERATIONS):
        sql = QUERIES[i % len(QUERIES)]
        scenario(rng, sql, bool(i % 2))
    if FAILURES:
        for item in FAILURES[:10]:
            print("FAIL " + item)
        print("%d failing check(s)" % len(FAILURES))
        return 1
    print("all %d differential scenarios passed" % ITERATIONS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
