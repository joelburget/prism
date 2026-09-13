"""Stable relational execution and semantics-preserving baseline optimization."""
from model import Expr
from parser import AGGREGATES
from binder import walk


def evaluate(e, row, group=None):
    if e.op == 'lit': return e.value
    if e.op == 'col': return row[e.index]
    if e.op in AGGREGATES:
        if e.op == 'COUNT': return len(group)
        vs = [evaluate(e.args[0], r) for r in group]
        return {'SUM': sum, 'MIN': min, 'MAX': max}[e.op](vs)
    a = evaluate(e.args[0], row, group)
    if e.op == 'NOT': return not a
    b = evaluate(e.args[1], row, group)
    if e.op == '+': return a + b
    if e.op == '-': return a - b
    if e.op == '*': return a * b
    if e.op == 'AND': return a and b
    if e.op == 'OR': return a or b
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


def optimize(plan):
    q = plan.query
    q.select = [(fold(e), a) for e, a in q.select]
    q.joins = [fold(e) for e in q.joins]
    if q.having: q.having = fold(q.having)
    if q.where:
        remaining = []
        for e in conjuncts(fold(q.where)):
            sources = {n.source for n in walk(e) if n.op == 'col'}
            if len(plan.tables) > 1 and len(sources) == 1:
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
    for right, on in zip(scans[1:], q.joins):
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
    for i, descending in reversed(q.order): projected.sort(key=lambda r: r[i], reverse=descending)
    projected = projected[q.offset: None if q.limit is None else q.offset + q.limit]
    return {'columns': [a for _, a in q.select], 'rows': projected}
