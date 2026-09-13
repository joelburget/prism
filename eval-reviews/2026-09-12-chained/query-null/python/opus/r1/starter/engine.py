"""Stable relational execution and semantics-preserving optimization.

Values are Python objects; SQL NULL is None and UNKNOWN is a None boolean, so
every scalar operator below implements three-valued logic explicitly.
"""
from model import Expr
from parser import AGGREGATES
from binder import walk


class Slots(dict):
    """Precomputed aggregate values, keyed by aggregate node identity.

    Batch execution evaluates an aggregate over the list of rows in its group;
    incremental maintenance instead keeps a running accumulator per group and
    hands the finished values to the same expression evaluator through this map.
    """


def evaluate(e, row, group=None):
    op = e.op
    if op == 'lit': return e.value
    if op == 'col': return row[e.index]
    if op == 'COUNT':
        if type(group) is Slots: return group[id(e)]
        if not e.args: return len(group)
        return sum(1 for r in group if evaluate(e.args[0], r) is not None)
    if op in ('SUM', 'MIN', 'MAX'):
        if type(group) is Slots: return group[id(e)]
        vs = [v for v in (evaluate(e.args[0], r) for r in group) if v is not None]
        return {'SUM': sum, 'MIN': min, 'MAX': max}[op](vs) if vs else None
    if op == 'COALESCE':
        for a in e.args:
            v = evaluate(a, row, group)
            if v is not None: return v
        return None
    a = evaluate(e.args[0], row, group)
    if op == 'IS NULL': return a is None
    if op == 'IS NOT NULL': return a is not None
    if op == 'NOT': return None if a is None else not a
    b = evaluate(e.args[1], row, group)
    if op == 'AND':
        if a is False or b is False: return False
        return None if a is None or b is None else True
    if op == 'OR':
        if a is True or b is True: return True
        return None if a is None or b is None else False
    if a is None or b is None: return None
    if op == '+': return a + b
    if op == '-': return a - b
    if op == '*': return a * b
    if op == '=': return a == b
    if op == '<>': return a != b
    if op == '<': return a < b
    if op == '<=': return a <= b
    if op == '>': return a > b
    if op == '>=': return a >= b
    raise AssertionError(op)


def true(v):
    """WHERE, ON, and HAVING retain TRUE only; FALSE and UNKNOWN are dropped."""
    return v is True


def literal(value, type):
    return Expr('lit', value, type=type, nullable=value is None)


def same(a, b):
    """Structural equality of two deterministic scalar expressions."""
    return (a.op == b.op and a.type == b.type and a.value == b.value and a.index == b.index
            and len(a.args) == len(b.args) and all(same(x, y) for x, y in zip(a.args, b.args)))


def fold(e):
    """Bottom-up rewriting; every rule must hold under three-valued logic."""
    e.args = [fold(a) for a in e.args]
    if e.op in AGGREGATES or e.op in ('lit', 'col'): return e
    if all(a.op == 'lit' for a in e.args):
        return literal(evaluate(e, []), e.type)
    return simplify(e)


def simplify(e):
    x, y = (e.args + [None, None])[:2]
    # IS [NOT] NULL on an operand that can never be NULL.
    if e.op == 'IS NULL' and not x.nullable: return literal(False, 'bool')
    if e.op == 'IS NOT NULL' and not x.nullable: return literal(True, 'bool')
    if e.op == 'AND':
        # FALSE annihilates UNKNOWN, and TRUE is the identity, in three-valued logic.
        if x.op == 'lit' and x.value is False or y.op == 'lit' and y.value is False:
            return literal(False, 'bool')
        if x.op == 'lit' and x.value is True: return y
        if y.op == 'lit' and y.value is True: return x
        if contradiction(x, y) or contradiction(y, x): return literal(False, 'bool')
    if e.op == 'OR':
        if x.op == 'lit' and x.value is True or y.op == 'lit' and y.value is True:
            return literal(True, 'bool')
        if x.op == 'lit' and x.value is False: return y
        if y.op == 'lit' and y.value is False: return x
        # p OR NOT p is UNKNOWN when p is NULL, so require a non-nullable p.
        if contradiction(x, y) or contradiction(y, x): return literal(True, 'bool')
    # x <op> x is UNKNOWN when x is NULL, so it folds only for non-nullable x.
    if e.op in ('=', '<>', '<', '<=', '>', '>=') and not x.nullable and not y.nullable and same(x, y):
        return literal(e.op in ('=', '<=', '>='), 'bool')
    return e


def contradiction(x, y):
    """True when y is NOT x and x can never be UNKNOWN."""
    return y.op == 'NOT' and not x.nullable and same(x, y.args[0])


def conjuncts(e):
    if e.op == 'AND': return conjuncts(e.args[0]) + conjuncts(e.args[1])
    return [e]


def rejects_null(e, width):
    """True when the predicate cannot be TRUE for an all-NULL (padded) row."""
    return not true(evaluate(e, [None] * width))


def optimize(plan):
    q = plan.query
    q.select = [(fold(e), a) for e, a in q.select]
    for j in q.joins: j.on = fold(j.on)
    if q.having: q.having = fold(q.having)
    if q.where:
        width = sum(len(t['columns']) for t in plan.tables)
        remaining = []
        for e in conjuncts(fold(q.where)):
            sources = {n.source for n in walk(e) if n.op == 'col'}
            if len(plan.tables) > 1 and len(sources) == 1:
                s = next(iter(sources))
                # The first source is always preserved, and the right input of an
                # inner join may be filtered before the join. The right input of a
                # left join may not, unless the filter discards padded rows anyway;
                # then the join is equivalent to an inner join.
                if s == 0 or not q.joins[s - 1].outer:
                    plan.filters[s].append(e)
                    continue
                if rejects_null(e, width):
                    q.joins[s - 1].outer = False
                    plan.filters[s].append(e)
                    continue
            remaining.append(e)
        q.where = None
        for e in remaining:
            q.where = e if q.where is None else Expr('AND', args=[q.where, e], type='bool')
    return plan


def execute(plan):
    q = plan.query
    scans, widths, offset = [], [], 0
    for t, predicates in zip(plan.tables, plan.filters):
        scans.append([r for r in t['rows'] if all(true(evaluate(p, [None] * offset + r)) for p in predicates)])
        widths.append(len(t['columns']))
        offset += widths[-1]
    rows = scans[0]
    for right, width, join in zip(scans[1:], widths[1:], q.joins):
        joined = []
        for l in rows:
            matched = False
            for r in right:
                if true(evaluate(join.on, l + r)):
                    joined.append(l + r)
                    matched = True
            if join.outer and not matched: joined.append(l + [None] * width)
        rows = joined
    if q.where: rows = [r for r in rows if true(evaluate(q.where, r))]
    if plan.aggregate:
        groups = {}
        for row in rows:
            key = tuple(evaluate(e, row) for e in q.groups)
            groups.setdefault(key, []).append(row)
        if not q.groups and not rows: groups[()] = []
        units = [(g[0] if g else [], g) for g in groups.values()]
    else: units = [(r, None) for r in rows]
    projected = [[evaluate(e, r, g) for e, _ in q.select] for r, g in units if q.having is None or true(evaluate(q.having, r, g))]
    return finish(q, projected)


def finish(q, projected):
    """DISTINCT, ORDER BY and OFFSET/LIMIT over already projected rows.

    Shared by batch execution and by reads of an incrementally maintained view,
    so both produce identical ordering.
    """
    if q.distinct: projected = [list(r) for r in dict.fromkeys(tuple(r) for r in projected)]
    for o in reversed(q.order):
        first = o.descending if o.nulls_first is None else o.nulls_first
        nulls = [r for r in projected if r[o.index] is None]
        values = [r for r in projected if r[o.index] is not None]
        values.sort(key=lambda r: r[o.index], reverse=o.descending)
        projected = nulls + values if first else values + nulls
    projected = projected[q.offset: None if q.limit is None else q.offset + q.limit]
    return {'columns': [a for _, a in q.select], 'rows': projected}
