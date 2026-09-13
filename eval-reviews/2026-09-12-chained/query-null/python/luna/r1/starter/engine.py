"""Stable relational execution and semantics-preserving baseline optimization."""
from model import Expr
from parser import AGGREGATES
from binder import walk


def evaluate(e, row, group=None):
    if e.op == 'lit': return e.value
    if e.op == 'col': return row[e.index]
    if e.op in AGGREGATES:
        if e.op == 'COUNT':
            if not e.args: return len(group)
            return sum(evaluate(e.args[0], r) is not None for r in group)
        vs = [evaluate(e.args[0], r) for r in group]
        vs = [v for v in vs if v is not None]
        if not vs: return None
        return {'SUM': sum, 'MIN': min, 'MAX': max}[e.op](vs)
    a = evaluate(e.args[0], row, group)
    if e.op == 'ISNULL': return a is None
    if e.op == 'ISNOTNULL': return a is not None
    if e.op == 'NOT': return None if a is None else not a
    b = evaluate(e.args[1], row, group)
    if e.op == 'COALESCE':
        for x in [a,b]+[evaluate(x,row,group) for x in e.args[2:]]:
            if x is not None: return x
        return None
    if e.op in ('+', '-', '*') and (a is None or b is None): return None
    if e.op in ('=', '<>', '<', '<=', '>', '>=') and (a is None or b is None): return None
    if e.op == '+': return a + b
    if e.op == '-': return a - b
    if e.op == '*': return a * b
    if e.op == 'AND': return False if a is False or b is False else (None if a is None or b is None else True)
    if e.op == 'OR': return True if a is True or b is True else (None if a is None or b is None else False)
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
    q.joins = [(fold(e), left) for e, left in q.joins]
    if q.having: q.having = fold(q.having)
    if q.where:
        remaining = []
        for e in conjuncts(fold(q.where)):
            sources = {n.source for n in walk(e) if n.op == 'col'}
            safe = all(not left for _, left in q.joins[:next(iter(sources))]) if sources else False
            if len(plan.tables) > 1 and len(sources) == 1 and safe:
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
    for join_no, (right, on) in enumerate(zip(scans[1:], q.joins)):
        on, is_left = on
        joined=[]
        nulls=[None] * len(plan.tables[join_no+1]['columns'])
        for l in rows:
            matches=[l+r for r in right if evaluate(on,l+r) is True]
            joined.extend(matches)
            if is_left and not matches: joined.append(l+nulls)
        rows=joined
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
    for i, descending, null_first in reversed(q.order):
        nf = descending if null_first is None else null_first
        # Sort values first, then bucket NULLs.  The second stable sort must
        # not disturb the requested value direction within either bucket.
        projected.sort(key=lambda r: (r[i] is None, r[i] if r[i] is not None else 0), reverse=descending)
        projected.sort(key=lambda r: 0 if r[i] is None else 1, reverse=not nf)
    projected = projected[q.offset: None if q.limit is None else q.offset + q.limit]
    return {'columns': [a for _, a in q.select], 'rows': projected}
