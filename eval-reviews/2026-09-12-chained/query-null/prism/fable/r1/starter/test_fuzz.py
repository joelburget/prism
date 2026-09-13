#!/usr/bin/env python3
"""Differential fuzz test for the query-null engine.

Generates random nullable databases and queries (inner/left joins,
three-valued predicates, IS NULL, COALESCE, grouping, global aggregates,
DISTINCT, ORDER BY with NULLS placement, LIMIT/OFFSET), evaluates them with a
small independent Python oracle that follows PROBLEM.md, and checks that the
engine agrees in both optimizer modes.

Usage: python3 test_fuzz.py [--seed N] [--count N] [--command ./run.sh]
"""
import argparse
import functools
import json
import random
import subprocess
import sys

TYPES = ["int", "text", "bool"]


# ---------------------------------------------------------------- database
def gen_database(rng):
    tables = []
    for ti in range(3):
        cols = [("id", "int", rng.random() < 0.5)]
        for ci in range(rng.randint(1, 3)):
            cols.append((f"c{ci}", rng.choice(TYPES), rng.random() < 0.7))
        rows = []
        for _ in range(rng.randint(0, 6)):
            row = []
            for (_, t, nullable) in cols:
                if nullable and rng.random() < 0.3:
                    row.append(None)
                elif t == "int":
                    row.append(rng.randint(-3, 5))
                elif t == "text":
                    row.append(rng.choice(["a", "b", "c", "ab", ""]))
                else:
                    row.append(rng.random() < 0.5)
            rows.append(row)
        tables.append({"name": f"t{ti}", "columns": [
            {"name": n, "type": t, "nullable": nl} for (n, t, nl) in cols], "rows": rows})
    return tables


# ---------------------------------------------------------------- expressions
# Expr forms: ('col', src, idx), ('lit', value), ('bin', op, a, b), ('not', a),
# ('is', a, negated), ('coalesce', [args]), ('agg', op, arg_or_None)
class Scope:
    def __init__(self, sources):
        # sources: list of (alias, cols[(name,type,nullable)], base_offset)
        self.sources = sources
        self.columns = []
        for si, (alias, cols, base) in enumerate(sources):
            for ci, (n, t, nl) in enumerate(cols):
                self.columns.append((si, ci, alias, n, t, base + ci))

    def columns_of_type(self, t):
        return [c for c in self.columns if c[4] == t]


def gen_expr(rng, scope, t, depth, allowed_cols=None, agg_ok=False):
    """Generate an expression of type t. allowed_cols restricts column refs
    (used for grouped queries). agg_ok permits aggregates at this level."""
    cols = scope.columns_of_type(t)
    if allowed_cols is not None:
        cols = [c for c in cols if c[5] in allowed_cols]
    choices = ["lit"]
    if cols:
        choices += ["col", "col", "col"]
    if depth > 0:
        choices += ["coalesce", "bin", "bin"]
        if t == "bool":
            choices += ["not", "is", "cmp", "cmp"]
    if agg_ok and depth > 0 and t in ("int", "text"):
        choices += ["agg", "agg"]
    if agg_ok and depth > 0 and t == "bool":
        choices += ["aggcmp"]
    k = rng.choice(choices)
    if k == "lit":
        if rng.random() < 0.2:
            return ("lit", None)
        if t == "int":
            return ("lit", rng.randint(-3, 5))
        if t == "text":
            return ("lit", rng.choice(["a", "b", "c", "ab", "it's"]))
        return ("lit", rng.random() < 0.5)
    if k == "col":
        c = rng.choice(cols)
        return ("col", c[0], c[1])
    if k == "coalesce":
        n = rng.randint(2, 3)
        return ("coalesce", [gen_expr(rng, scope, t, depth - 1, allowed_cols, agg_ok) for _ in range(n)])
    if k == "bin":
        if t == "int":
            op = rng.choice(["+", "-", "*"])
        elif t == "bool":
            op = rng.choice(["AND", "OR"])
        else:
            return gen_expr(rng, scope, t, 0, allowed_cols, agg_ok)
        return ("bin", op, gen_expr(rng, scope, t, depth - 1, allowed_cols, agg_ok),
                gen_expr(rng, scope, t, depth - 1, allowed_cols, agg_ok))
    if k == "not":
        return ("not", gen_expr(rng, scope, "bool", depth - 1, allowed_cols, agg_ok))
    if k == "is":
        ot = rng.choice(TYPES)
        return ("is", gen_expr(rng, scope, ot, depth - 1, allowed_cols, agg_ok), rng.random() < 0.5)
    if k == "cmp":
        ot = rng.choice(TYPES)
        op = rng.choice(["=", "<>"]) if ot == "bool" else rng.choice(["=", "<>", "<", "<=", ">", ">="])
        return ("bin", op, gen_expr(rng, scope, ot, depth - 1, allowed_cols, agg_ok),
                gen_expr(rng, scope, ot, depth - 1, allowed_cols, agg_ok))
    if k == "agg":
        op = rng.choice(["SUM", "MIN", "MAX"]) if t == "int" else rng.choice(["MIN", "MAX"])
        return ("agg", op, gen_expr(rng, scope, t, depth - 1, None, False))
    if k == "aggcmp":
        if rng.random() < 0.5:
            arg = None if rng.random() < 0.5 else gen_expr(rng, scope, rng.choice(TYPES), depth - 1, None, False)
            left = ("agg", "COUNT", arg)
        else:
            left = ("agg", rng.choice(["SUM", "MIN", "MAX"]), gen_expr(rng, scope, "int", depth - 1, None, False))
        return ("bin", rng.choice(["=", "<>", "<", "<=", ">", ">="]), left, ("lit", rng.randint(-2, 4)))
    raise AssertionError(k)


def render(e, scope):
    k = e[0]
    if k == "col":
        alias = scope.sources[e[1]][0]
        return f"{alias}.{scope.sources[e[1]][1][e[2]][0]}"
    if k == "lit":
        v = e[1]
        if v is None:
            return "NULL"
        if isinstance(v, bool):
            return "TRUE" if v else "FALSE"
        if isinstance(v, int):
            return str(v)
        return "'" + v.replace("'", "''") + "'"
    if k == "bin":
        return f"({render(e[2], scope)} {e[1]} {render(e[3], scope)})"
    if k == "not":
        return f"(NOT {render(e[1], scope)})"
    if k == "is":
        return f"({render(e[1], scope)} IS {'NOT ' if e[2] else ''}NULL)"
    if k == "coalesce":
        return "COALESCE(" + ", ".join(render(a, scope) for a in e[1]) + ")"
    if k == "agg":
        if e[2] is None:
            return "COUNT(*)"
        return f"{e[1]}({render(e[2], scope)})"
    raise AssertionError(k)


def slot(scope, e):
    return scope.sources[e[1]][2] + e[2]


def evaluate(e, scope, row, group):
    k = e[0]
    if k == "col":
        return row[slot(scope, e)]
    if k == "lit":
        return e[1]
    if k == "coalesce":
        for a in e[1]:
            v = evaluate(a, scope, row, group)
            if v is not None:
                return v
        return None
    if k == "not":
        v = evaluate(e[1], scope, row, group)
        return None if v is None else (not v)
    if k == "is":
        v = evaluate(e[1], scope, row, group)
        return (v is not None) if e[2] else (v is None)
    if k == "agg":
        if e[2] is None:
            return len(group)
        vs = [evaluate(e[2], scope, r, []) for r in group]
        vs = [v for v in vs if v is not None]
        if e[1] == "COUNT":
            return len(vs)
        if not vs:
            return None
        if e[1] == "SUM":
            return sum(vs)
        return min(vs) if e[1] == "MIN" else max(vs)
    op = e[1]
    a = evaluate(e[2], scope, row, group)
    b = evaluate(e[3], scope, row, group)
    if op == "AND":
        if a is False or b is False:
            return False
        if a is None or b is None:
            return None
        return True
    if op == "OR":
        if a is True or b is True:
            return True
        if a is None or b is None:
            return None
        return False
    if a is None or b is None:
        return None
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    if op == "*":
        return a * b
    if isinstance(a, str):
        ka, kb = a.encode(), b.encode()
    else:
        ka, kb = a, b
    return {"=": ka == kb, "<>": ka != kb, "<": ka < kb, "<=": ka <= kb, ">": ka > kb, ">=": ka >= kb}[op]


# ---------------------------------------------------------------- queries
def gen_query(rng, db):
    n = rng.choice([1, 1, 2, 2, 3])
    picks = rng.sample(range(3), n)
    sources = []
    base = 0
    for i, ti in enumerate(picks):
        cols = [(c["name"], c["type"], c["nullable"]) for c in db[ti]["columns"]]
        sources.append((f"s{i}", cols, base))
        base += len(cols)
    scope = Scope(sources)
    joins = []
    for i in range(1, n):
        left_cols = [c for c in scope.columns if c[0] < i and c[4] == "int"]
        right_cols = [c for c in scope.columns if c[0] == i and c[4] == "int"]
        if left_cols and right_cols and rng.random() < 0.8:
            lc, rc = rng.choice(left_cols), rng.choice(right_cols)
            on = ("bin", "=", ("col", lc[0], lc[1]), ("col", rc[0], rc[1]))
            if rng.random() < 0.3:
                sub = Scope(sources[: i + 1])
                on = ("bin", "AND", on, gen_expr(rng, sub, "bool", 1))
        else:
            on = gen_expr(rng, Scope(sources[: i + 1]), "bool", 1)
        joins.append((rng.random() < 0.6, on))
    where = gen_expr(rng, scope, "bool", 2) if rng.random() < 0.8 else None
    grouped = rng.random() < 0.35
    global_agg = not grouped and rng.random() < 0.15
    group_keys = []
    if grouped:
        group_keys = rng.sample(scope.columns, rng.randint(1, min(2, len(scope.columns))))
    selects = []
    aliases = []
    for i in range(rng.randint(1, 3)):
        t = rng.choice(TYPES)
        if grouped:
            allowed = {c[5] for c in group_keys}
            e = gen_expr(rng, scope, t, 2, allowed, True)
        elif global_agg:
            e = gen_expr(rng, scope, t, 2, set(), True)
        else:
            e = gen_expr(rng, scope, t, 2)
        selects.append(e)
        aliases.append(f"a{i}")
    if grouped and rng.random() < 0.5:
        selects.append(("col", group_keys[0][0], group_keys[0][1]))
        aliases.append("k")
    if global_agg and not any(has_agg(e) for e in selects):
        selects.append(("agg", "COUNT", None))
        aliases.append("n")
    having = None
    if (grouped or global_agg) and rng.random() < 0.5:
        allowed = {c[5] for c in group_keys}
        having = gen_expr(rng, scope, "bool", 2, allowed, True)
    distinct = rng.random() < 0.2
    order = []
    if rng.random() < 0.6:
        for a in rng.sample(aliases, rng.randint(1, len(aliases))):
            order.append((a, rng.choice([None, "ASC", "DESC"]), rng.choice([None, None, "FIRST", "LAST"])))
    limit = rng.randint(0, 5) if rng.random() < 0.3 else None
    offset = rng.randint(0, 3) if limit is not None and rng.random() < 0.5 else None
    return dict(picks=picks, scope=scope, joins=joins, where=where, group_keys=group_keys,
                global_agg=global_agg, selects=selects, aliases=aliases, having=having,
                distinct=distinct, order=order, limit=limit, offset=offset)


def render_query(q, db):
    sc = q["scope"]
    parts = ["SELECT"]
    if q["distinct"]:
        parts.append("DISTINCT")
    parts.append(", ".join(f"{render(e, sc)} AS {a}" for e, a in zip(q["selects"], q["aliases"])))
    parts.append(f"FROM {db[q['picks'][0]]['name']} AS s0")
    for i, (outer, on) in enumerate(q["joins"], start=1):
        kw = "LEFT JOIN" if outer else "INNER JOIN"
        parts.append(f"{kw} {db[q['picks'][i]]['name']} AS s{i} ON {render(on, sc)}")
    if q["where"] is not None:
        parts.append("WHERE " + render(q["where"], sc))
    if q["group_keys"]:
        parts.append("GROUP BY " + ", ".join(render(("col", c[0], c[1]), sc) for c in q["group_keys"]))
    if q["having"] is not None:
        parts.append("HAVING " + render(q["having"], sc))
    if q["order"]:
        items = []
        for a, d, nl in q["order"]:
            s = a
            if d:
                s += " " + d
            if nl:
                s += " NULLS " + nl
            items.append(s)
        parts.append("ORDER BY " + ", ".join(items))
    if q["limit"] is not None:
        parts.append(f"LIMIT {q['limit']}")
        if q["offset"] is not None:
            parts.append(f"OFFSET {q['offset']}")
    return " ".join(parts)


def sort_key_value(v):
    if isinstance(v, bool):
        return (0, int(v))
    if isinstance(v, int):
        return (0, v)
    return (0, v.encode())


def oracle(q, db):
    sc = q["scope"]
    rows = [list(r) for r in db[q["picks"][0]]["rows"]]
    for i, (outer, on) in enumerate(q["joins"], start=1):
        right = db[q["picks"][i]]["rows"]
        width = len(db[q["picks"][i]]["columns"])
        out = []
        for l in rows:
            matched = False
            for r in right:
                cand = l + list(r)
                if evaluate(on, sc, cand, []) is True:
                    out.append(cand)
                    matched = True
            if outer and not matched:
                out.append(l + [None] * width)
        rows = out
    if q["where"] is not None:
        rows = [r for r in rows if evaluate(q["where"], sc, r, []) is True]
    aggregate = bool(q["group_keys"]) or any(
        has_agg(e) for e in q["selects"]) or (q["having"] is not None and has_agg(q["having"]))
    projected = []
    if aggregate:
        groups = []
        keys = {}
        for r in rows:
            key = json.dumps([r[c[5]] for c in q["group_keys"]])
            if key not in keys:
                keys[key] = len(groups)
                groups.append([])
            groups[keys[key]].append(r)
        if not q["group_keys"] and not rows:
            groups = [[]]
        for g in groups:
            row = g[0] if g else []
            if q["having"] is not None and evaluate(q["having"], sc, row, g) is not True:
                continue
            projected.append([evaluate(e, sc, row, g) for e in q["selects"]])
    else:
        for r in rows:
            projected.append([evaluate(e, sc, r, []) for e in q["selects"]])
    if q["distinct"]:
        seen = set()
        kept = []
        for r in projected:
            k = json.dumps(r)
            if k not in seen:
                seen.add(k)
                kept.append(r)
        projected = kept
    def compare(x, y):
        for a, d, nl in q["order"]:
            idx = q["aliases"].index(a)
            desc = d == "DESC"
            nulls_first = desc if nl is None else nl == "FIRST"
            u, v = x[idx], y[idx]
            if u is None and v is None:
                continue
            if u is None:
                return -1 if nulls_first else 1
            if v is None:
                return 1 if nulls_first else -1
            ku, kv = sort_key_value(u), sort_key_value(v)
            if ku == kv:
                continue
            c = -1 if ku < kv else 1
            return -c if desc else c
        return 0
    if q["order"]:
        projected.sort(key=functools.cmp_to_key(compare))
    if q["limit"] is not None:
        off = q["offset"] or 0
        projected = projected[off: off + q["limit"]]
    return {"columns": list(q["aliases"]), "rows": projected}


def has_agg(e):
    if e[0] == "agg":
        return True
    if e[0] in ("bin",):
        return has_agg(e[2]) or has_agg(e[3])
    if e[0] in ("not",):
        return has_agg(e[1])
    if e[0] == "is":
        return has_agg(e[1])
    if e[0] == "coalesce":
        return any(has_agg(a) for a in e[1])
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--count", type=int, default=40)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--command", default="./run.sh")
    args = ap.parse_args()
    rng = random.Random(args.seed)
    failures = 0
    total = 0
    for _ in range(args.count):
        db = gen_database(rng)
        queries = [gen_query(rng, db) for _ in range(args.batch)]
        request = {"protocol_version": 1, "task": "query-null", "input": {
            "database": db,
            "queries": [{"sql": render_query(q, db), "optimize": opt} for q in queries for opt in (False, True)]}}
        expected = [oracle(q, db) for q in queries for _ in (False, True)]
        proc = subprocess.run([args.command], input=json.dumps(request).encode(), capture_output=True)
        try:
            resp = json.loads(proc.stdout.decode())
        except Exception:
            print("BAD OUTPUT", proc.stdout[:300], proc.stderr[:300])
            failures += 1
            continue
        if not resp.get("ok"):
            failures += 1
            for qq in request["input"]["queries"]:
                single = dict(request, input={"database": db, "queries": [qq]})
                out = subprocess.run([args.command], input=json.dumps(single).encode(), capture_output=True).stdout
                if not json.loads(out.decode()).get("ok"):
                    print("ERROR RESPONSE", out.decode().strip(), "for", qq["sql"])
                    break
            continue
        got = resp["result"]["results"]
        for i, (e, g) in enumerate(zip(expected, got)):
            total += 1
            if e != g:
                failures += 1
                print("MISMATCH", request["input"]["queries"][i]["sql"], "optimize" if i % 2 else "plain")
                print("  db:", json.dumps(db))
                print("  expected:", json.dumps(e))
                print("  got:     ", json.dumps(g))
    print(f"{total} query results checked, {failures} failures")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
