#!/usr/bin/env python3
"""Differential fuzz test for incrementally maintained views (checkpoint two).

Reuses the database/query generator and the independent Python oracle of
test_fuzz.py. Each trial creates a few views over a random nullable database,
then applies random change batches (inserts, updates, deletes, including
batches that must fail and roll back), reads every view after each batch, and
occasionally drops and recreates views. Every read is compared against the
oracle evaluated on the table contents in encounter order, and revisions and
error codes are checked against a simulation of the command rules.

Usage: python3 test_views.py [--seed N] [--count N] [--command ./run.sh]
"""
import argparse
import copy
import json
import random
import subprocess
import sys

import test_fuzz as tf

BOUND = 1_000_000_000


def gen_value(rng, kind, nullable, valid):
    if valid and nullable and rng.random() < 0.3:
        return None
    if not valid:
        choice = rng.random()
        if choice < 0.4 and not nullable:
            return None
        if choice < 0.7:
            return {"int": "x", "text": 1, "bool": 0}[kind]
        if kind == "int":
            return BOUND + 1
        return {"text": True, "bool": "true"}[kind]
    if kind == "int":
        return rng.randint(-3, 5)
    if kind == "text":
        return rng.choice(["a", "b", "c", "ab", ""])
    return rng.random() < 0.5


def gen_row(rng, cols, valid=True):
    row = [gen_value(rng, c["type"], c["nullable"], True) for c in cols]
    if not valid:
        if rng.random() < 0.2:
            return row + [1] if rng.random() < 0.5 else row[:-1]
        i = rng.randrange(len(cols))
        row[i] = gen_value(rng, cols[i]["type"], cols[i]["nullable"], False)
    return row


class Sim:
    """The database as the command rules describe it."""

    def __init__(self, db):
        self.schema = db
        # per table: ordered list of [id, row]; used ids; next id hint
        self.tables = [[[i + 1, list(r)] for i, r in enumerate(t["rows"])] for t in db]
        self.used = [set(range(1, len(t["rows"]) + 1)) for t in db]
        self.revision = 0

    def current_db(self):
        return [dict(t, rows=[r for _, r in rows]) for t, rows in zip(self.schema, self.tables)]

    def valid_row(self, ti, row):
        cols = self.schema[ti]["columns"]
        if not isinstance(row, list) or len(row) != len(cols):
            return False
        for v, c in zip(row, cols):
            if v is None:
                if not c["nullable"]:
                    return False
            elif c["type"] == "int":
                if isinstance(v, bool) or not isinstance(v, int) or abs(v) > BOUND:
                    return False
            elif c["type"] == "bool":
                if not isinstance(v, bool):
                    return False
            elif not isinstance(v, str):
                return False
        return True

    def apply(self, changes):
        tables = copy.deepcopy(self.tables)
        used = copy.deepcopy(self.used)
        names = [t["name"] for t in self.schema]
        for ch in changes:
            if ch["table"] not in names:
                return "UNKNOWN_TABLE"
            ti = names.index(ch["table"])
            if ch["op"] in ("insert", "update") and not self.valid_row(ti, ch["row"]):
                return "INVALID_ROW"
            ids = [i for i, _ in tables[ti]]
            if ch["op"] == "insert":
                if ch["id"] in used[ti]:
                    return "ROW_ID_USED"
                used[ti].add(ch["id"])
                tables[ti].append([ch["id"], list(ch["row"])])
            elif ch["id"] not in ids:
                return "UNKNOWN_ROW"
            elif ch["op"] == "update":
                tables[ti][ids.index(ch["id"])][1] = list(ch["row"])
            else:
                del tables[ti][ids.index(ch["id"])]
        self.tables, self.used = tables, used
        self.revision += 1
        return None


def gen_change(rng, sim):
    if rng.random() < 0.03:
        return {"op": "delete", "table": "nope", "id": 1}
    ti = rng.randrange(len(sim.schema))
    table = sim.schema[ti]
    live = [i for i, _ in sim.tables[ti]]
    kind = rng.choice(["insert", "insert", "update", "update", "delete", "delete"])
    if kind == "insert":
        if sim.used[ti] and rng.random() < 0.08:
            rid = rng.choice(sorted(sim.used[ti]))
        else:
            rid = rng.choice([max(sim.used[ti] | {0}) + 1, rng.randint(1, 60)])
        return {"op": "insert", "table": table["name"], "id": rid,
                "row": gen_row(rng, table["columns"], rng.random() > 0.06)}
    if live and rng.random() > 0.08:
        rid = rng.choice(live)
    else:
        rid = rng.randint(1, 60)
    if kind == "update":
        return {"op": "update", "table": table["name"], "id": rid,
                "row": gen_row(rng, table["columns"], rng.random() > 0.06)}
    return {"op": "delete", "table": table["name"], "id": rid}


def ok(value):
    return {"ok": True, "result": value}


def err(code):
    return {"ok": False, "error": {"code": code}}


def run_trial(rng, command):
    db = tf.gen_database(rng)
    sim = Sim(db)
    queries = [tf.gen_query(rng, db) for _ in range(rng.randint(1, 4))]
    commands, expected = [], []
    views = {}

    def create(name, q, optimize):
        commands.append({"op": "create", "view": name, "sql": tf.render_query(q, db), "optimize": optimize})
        expected.append(ok({"view": name, "revision": sim.revision}))
        views[name] = q

    def read_all():
        current = sim.current_db()
        for name, q in views.items():
            commands.append({"op": "read", "view": name})
            result = tf.oracle(q, current)
            expected.append(ok({"revision": sim.revision, "columns": result["columns"], "rows": result["rows"]}))

    for i, q in enumerate(queries):
        create(f"v{i}", q, rng.random() < 0.5)
    read_all()
    for _ in range(rng.randint(3, 12)):
        changes = [gen_change(rng, sim) for _ in range(rng.randint(1, 5))]
        # a batch may touch the same row several times
        if rng.random() < 0.3 and changes[0]["op"] == "insert":
            follow = rng.choice(["update", "delete"])
            extra = {"op": follow, "table": changes[0]["table"], "id": changes[0]["id"]}
            if follow == "update":
                cols = next(t for t in db if t["name"] == changes[0]["table"])["columns"]
                extra["row"] = gen_row(rng, cols)
            changes.append(extra)
        commands.append({"op": "apply", "changes": changes})
        code = sim.apply(changes)
        expected.append(err(code) if code else ok({"revision": sim.revision}))
        read_all()
        roll = rng.random()
        if roll < 0.12 and views:
            name = rng.choice(sorted(views))
            commands.append({"op": "drop", "view": name})
            expected.append(ok({"dropped": name}))
            del views[name]
            if rng.random() < 0.7:
                create(name, tf.gen_query(rng, db), rng.random() < 0.5)
                read_all()
        elif roll < 0.16:
            commands.append({"op": "read", "view": "missing"})
            expected.append(err("UNKNOWN_VIEW"))
        elif roll < 0.2 and views:
            name = rng.choice(sorted(views))
            commands.append({"op": "create", "view": name, "sql": "SELECT 1 AS x FROM t0", "optimize": True})
            expected.append(err("VIEW_EXISTS"))
    request = {"protocol_version": 1, "task": "query-null", "input": {"database": db, "commands": commands}}
    proc = subprocess.run([command], input=json.dumps(request).encode(), capture_output=True)
    try:
        resp = json.loads(proc.stdout.decode())
    except Exception:
        print("BAD OUTPUT", proc.stdout[:300], proc.stderr[:300])
        return 1, 0
    if not resp.get("ok"):
        print("ERROR RESPONSE", json.dumps(resp), "for", json.dumps(request)[:2000])
        return 1, 0
    got = resp["result"]["results"]
    if len(got) != len(expected):
        print("LENGTH MISMATCH", len(got), len(expected))
        return 1, 0
    for i, (e, g) in enumerate(zip(expected, got)):
        if e != g:
            print("MISMATCH at command", i, json.dumps(commands[i])[:400])
            print("  views:", {n: tf.render_query(q, db) for n, q in views.items()})
            print("  db:", json.dumps(db))
            print("  commands:", json.dumps(commands[: i + 1]))
            print("  expected:", json.dumps(e))
            print("  got:     ", json.dumps(g))
            return 1, len(expected)
    return 0, len(expected)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--count", type=int, default=30)
    ap.add_argument("--command", default="./run.sh")
    args = ap.parse_args()
    rng = random.Random(args.seed)
    failures = total = 0
    for _ in range(args.count):
        f, n = run_trial(rng, args.command)
        failures += f
        total += n
    print(f"{total} replies checked, {failures} failing trials")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
