"""Stable relational execution and optimization with SQL three-valued logic."""
from model import Expr
from parser import AGGREGATES
from binder import walk


def evaluate(e, row, group=None):
    if e.op == 'lit': return e.value
    if e.op == 'col': return row[e.index]
    if e.op in AGGREGATES:
        if e.op == 'COUNT' and not e.args: return len(group)
        vs = [v for r in group if (v := evaluate(e.args[0], r)) is not None]
        if e.op == 'COUNT': return len(vs)
        if not vs: return None
        return {'SUM': sum, 'MIN': min, 'MAX': max}[e.op](vs)
    if e.op == 'COALESCE':
        for arg in e.args:
            value = evaluate(arg, row, group)
            if value is not None: return value
        return None
    a = evaluate(e.args[0], row, group)
    if e.op == 'IS NULL': return a is None
    if e.op == 'IS NOT NULL': return a is not None
    if e.op == 'NOT': return None if a is None else not a
    b = evaluate(e.args[1], row, group)
    if e.op == 'AND':
        if a is False or b is False: return False
        return None if a is None or b is None else True
    if e.op == 'OR':
        if a is True or b is True: return True
        return None if a is None or b is None else False
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
        value = evaluate(e, [])
        return Expr('lit', value, type=e.type, nullable=value is None, types=e.types.copy())
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
        # Only these sources can never be null-padded by our left-deep joins.
        safe_sources = {0} | {i + 1 for i, kind in enumerate(q.join_kinds) if kind == 'INNER'}
        for e in conjuncts(fold(q.where)):
            sources = {n.source for n in walk(e) if n.op == 'col'}
            # A WHERE filter on a null-padded source must stay above the join:
            # filtering its scan could create a new unmatched output row.
            if len(plan.tables) > 1 and len(sources) == 1 and sources <= safe_sources:
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
    for si, (right, on, kind) in enumerate(zip(scans[1:], q.joins, q.join_kinds), 1):
        joined = []
        for left in rows:
            matches = [left + r for r in right if evaluate(on, left + r) is True]
            if matches: joined.extend(matches)
            elif kind == 'LEFT': joined.append(left + [None] * len(plan.tables[si]['columns']))
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
    projected = [[evaluate(e, r, g) for e, _ in q.select] for r, g in units if q.having is None or evaluate(q.having, r, g) is True]
    if q.distinct: projected = [list(r) for r in dict.fromkeys(tuple(r) for r in projected)]
    for i, descending, nulls_first in reversed(q.order):
        # Sort values and NULL placement independently so explicit placement
        # remains independent of direction; both passes preserve ties.
        projected.sort(key=lambda r: (r[i] is not None, r[i]), reverse=descending)
        projected.sort(key=lambda r: (r[i] is not None) if nulls_first else (r[i] is None))
    projected = projected[q.offset: None if q.limit is None else q.offset + q.limit]
    return {'columns': [a for _, a in q.select], 'rows': projected}
