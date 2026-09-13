"""Stable relational execution and semantics-preserving baseline optimization."""
from model import Expr
from parser import AGGREGATES
from binder import walk
from functools import cmp_to_key


def evaluate(e, row, group=None):
    if e.op == 'lit': return e.value
    if e.op == 'col': return row[e.index]
    if e.op in AGGREGATES:
        if e.op == 'COUNT':
            return len(group) if not e.args else sum(evaluate(e.args[0], r) is not None for r in group)
        vs = [evaluate(e.args[0], r) for r in group]
        vs = [v for v in vs if v is not None]
        if not vs: return None
        return {'SUM': sum, 'MIN': min, 'MAX': max}[e.op](vs)
    a = evaluate(e.args[0], row, group)
    if e.op == 'NOT': return None if a is None else not a
    if e.op == 'IS NULL': return a is None
    if e.op == 'IS NOT NULL': return a is not None
    if e.op == 'COALESCE':
        for arg in e.args:
            v = evaluate(arg, row, group)
            if v is not None: return v
        return None
    b = evaluate(e.args[1], row, group)
    if e.op in ('+', '-', '*'):
        return None if a is None or b is None else {'+': lambda: a + b, '-': lambda: a - b, '*': lambda: a * b}[e.op]()
    if e.op == 'AND':
        return False if a is False or b is False else (None if a is None or b is None else True)
    if e.op == 'OR':
        return True if a is True or b is True else (None if a is None or b is None else False)
    if a is None or b is None: return None
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
    q.joins = [(kind, fold(e)) for kind, e in q.joins]
    if q.having: q.having = fold(q.having)
    if q.where:
        remaining = []
        for e in conjuncts(fold(q.where)):
            sources = {n.source for n in walk(e) if n.op == 'col'}
            if len(plan.tables) > 1 and all(kind == 'INNER' for kind, _ in q.joins) and len(sources) == 1:
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
        scans.append([r for r in t['rows'] if all(evaluate(p, [None] * offset + r) is True for p in predicates)])
        offset += len(t['columns'])
    rows = scans[0]
    for join_index, (right, (kind, on)) in enumerate(zip(scans[1:], q.joins)):
        padded = [None] * len(plan.tables[join_index + 1]['columns'])
        joined = []
        for l in rows:
            matches = [l + r for r in right if evaluate(on, l + r) is True]
            if matches: joined.extend(matches)
            elif kind == 'LEFT': joined.append(l + padded)
        rows = joined
    if q.where: rows = [r for r in rows if evaluate(q.where, r) is True]
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
    def compare(left, right):
        for i, descending, explicit in q.order:
            a, b = left[i], right[i]
            if a is None or b is None:
                if a is b: continue
                first = explicit == 'FIRST' if explicit else descending
                return -1 if (a is None) == first else 1
            if a != b: return (-1 if a < b else 1) * (-1 if descending else 1)
        return 0
    if q.order: projected.sort(key=cmp_to_key(compare))
    projected = projected[q.offset: None if q.limit is None else q.offset + q.limit]
    return {'columns': [a for _, a in q.select], 'rows': projected}
