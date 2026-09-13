"""Stable relational execution and semantics-preserving baseline optimization."""
from model import Expr
from parser import AGGREGATES
from binder import walk


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
    if e.op == 'COALESCE':
        for arg in e.args:
            value = evaluate(arg, row, group)
            if value is not None: return value
        return None
    a = evaluate(e.args[0], row, group)
    if e.op == 'IS_NULL': return a is None
    if e.op == 'IS_NOT_NULL': return a is not None
    if e.op == 'NOT': return None if a is None else not a
    b = evaluate(e.args[1], row, group)
    if e.op == 'AND':
        if a is False or b is False: return False
        if a is None or b is None: return None
        return True
    if e.op == 'OR':
        if a is True or b is True: return True
        if a is None or b is None: return None
        return False
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


def optimize(plan):
    q = plan.query
    q.select = [(fold(e), a) for e, a in q.select]
    q.joins = [(kind, fold(e)) for kind, e in q.joins]
    if q.having: q.having = fold(q.having)
    if q.where:
        remaining = []
        for e in conjuncts(fold(q.where)):
            sources = {n.source for n in walk(e) if n.op == 'col'}
            if len(plan.tables) > 1 and all(kind == 'inner' for kind, _ in q.joins) and len(sources) == 1:
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
    for table, right, (kind, on) in zip(plan.tables[1:], scans[1:], q.joins):
        joined = []
        padding = [None] * len(table['columns'])
        for left in rows:
            matches = [left + r for r in right if evaluate(on, left + r) is True]
            joined.extend(matches if matches else ([left + padding] if kind == 'left' else []))
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
    return format_projected(plan, projected)


def format_projected(plan, projected):
    """Apply the post-projection operators to cached view contributions."""
    q = plan.query
    projected = [list(r) for r in projected]
    if q.distinct: projected = [list(r) for r in dict.fromkeys(tuple(r) for r in projected)]
    for i, descending, explicit in reversed(q.order):
        null_first = explicit == 'first' if explicit else descending
        null_marker = 1 if (null_first == descending) else 0
        projected.sort(key=lambda r: (null_marker if r[i] is None else 1 - null_marker,
                                      0 if r[i] is None else r[i]), reverse=descending)
    projected = projected[q.offset: None if q.limit is None else q.offset + q.limit]
    return {'columns': [a for _, a in q.select], 'rows': projected}
