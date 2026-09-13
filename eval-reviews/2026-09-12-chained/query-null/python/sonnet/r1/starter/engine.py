"""Stable relational execution and semantics-preserving baseline optimization."""
import functools
from model import Expr
from parser import AGGREGATES
from binder import walk


def evaluate(e, row, group=None):
    if e.op == 'lit': return e.value
    if e.op == 'col': return row[e.index]
    if e.op == 'ISNULL': return evaluate(e.args[0], row, group) is None
    if e.op == 'ISNOTNULL': return evaluate(e.args[0], row, group) is not None
    if e.op == 'COALESCE':
        for a in e.args:
            v = evaluate(a, row, group)
            if v is not None: return v
        return None
    if e.op in AGGREGATES:
        if e.op == 'COUNT':
            if not e.args: return len(group)
            return sum(1 for r in group if evaluate(e.args[0], r) is not None)
        vs = [v for r in group for v in [evaluate(e.args[0], r)] if v is not None]
        if not vs: return None
        return {'SUM': sum, 'MIN': min, 'MAX': max}[e.op](vs)
    if e.op == 'NOT':
        a = evaluate(e.args[0], row, group)
        return None if a is None else not a
    a = evaluate(e.args[0], row, group)
    b = evaluate(e.args[1], row, group)
    if e.op == 'AND':
        if a is False or b is False: return False
        if a is None or b is None: return None
        return a and b
    if e.op == 'OR':
        if a is True or b is True: return True
        if a is None or b is None: return None
        return a or b
    if a is None or b is None: return None
    if e.op == '+': return a + b
    if e.op == '-': return a - b
    if e.op == '*': return a * b
    if e.op == '=': return a == b
    if e.op == '<>': return a != b
    if e.op == '<': return a < b
    if e.op == '<=': return a <= b
    if e.op == '>': return a > b
    if e.op == '>=': return a >= b
    raise AssertionError(e.op)


def fold(e):
    e.args = [fold(a) for a in e.args]
    if e.op not in AGGREGATES | {'lit', 'col'} and all(a.op == 'lit' for a in e.args):
        return Expr('lit', evaluate(e, []), type=e.type)
    return e


def conjuncts(e):
    if e.op == 'AND': return conjuncts(e.args[0]) + conjuncts(e.args[1])
    return [e]


def compare_value(a, b, desc, nulls):
    if a is None and b is None: return 0
    if nulls is None: nulls = 'FIRST' if desc else 'LAST'
    if a is None: return -1 if nulls == 'FIRST' else 1
    if b is None: return 1 if nulls == 'FIRST' else -1
    if a == b: return 0
    lt = a < b
    if desc: return 1 if lt else -1
    return -1 if lt else 1


def sort_rows(rows, order):
    def cmp(r1, r2):
        for i, desc, nulls in order:
            c = compare_value(r1[i], r2[i], desc, nulls)
            if c != 0: return c
        return 0
    return sorted(rows, key=functools.cmp_to_key(cmp))


def optimize(plan):
    q = plan.query
    q.select = [(fold(e), a) for e, a in q.select]
    q.joins = [fold(e) for e in q.joins]
    if q.having: q.having = fold(q.having)
    pushable = [True] + [k != 'LEFT' for k in q.join_kinds]
    if q.where:
        remaining = []
        for e in conjuncts(fold(q.where)):
            sources = {n.source for n in walk(e) if n.op == 'col'}
            if len(plan.tables) > 1 and len(sources) == 1 and pushable[next(iter(sources))]:
                plan.filters[next(iter(sources))].append(e)
            else: remaining.append(e)
        q.where = None
        for e in remaining:
            q.where = e if q.where is None else Expr('AND', args=[q.where, e], type='bool')
    return plan


def execute(plan):
    q = plan.query
    scans, offset = [], 0
    for t, predicates in zip(plan.tables, plan.filters):
        scans.append([r for r in t['rows'] if all(evaluate(p, [None] * offset + r) for p in predicates)])
        offset += len(t['columns'])
    rows = scans[0]
    for right, on, kind, width in zip(scans[1:], q.joins, q.join_kinds, (len(t['columns']) for t in plan.tables[1:])):
        if kind == 'LEFT':
            joined = []
            for l in rows:
                matched = False
                for r in right:
                    if evaluate(on, l + r):
                        joined.append(l + r)
                        matched = True
                if not matched:
                    joined.append(l + [None] * width)
            rows = joined
        else:
            rows = [l + r for l in rows for r in right if evaluate(on, l + r)]
    if q.where: rows = [r for r in rows if evaluate(q.where, r)]
    if plan.aggregate:
        groups = {}
        for row in rows:
            key = tuple(evaluate(e, row) for e in q.groups)
            groups.setdefault(key, []).append(row)
        if not q.groups and not rows: groups[()] = []
        units = [(g[0] if g else [], g) for g in groups.values()]
    else: units = [(r, None) for r in rows]
    projected = [[evaluate(e, r, g) for e, _ in q.select] for r, g in units if q.having is None or evaluate(q.having, r, g)]
    if q.distinct: projected = [list(r) for r in dict.fromkeys(tuple(r) for r in projected)]
    if q.order: projected = sort_rows(projected, q.order)
    projected = projected[q.offset: None if q.limit is None else q.offset + q.limit]
    return {'columns': [a for _, a in q.select], 'rows': projected}
