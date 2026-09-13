"""Stable relational execution and semantics-preserving optimization.

Values are Python int/str/bool, with SQL NULL represented as None. Predicates
follow three-valued logic: True, False, or None (UNKNOWN); only True passes a
WHERE, ON, or HAVING filter."""
from model import Expr
from parser import AGGREGATES
from binder import walk


def is_true(v):
    return v is True


def evaluate(e, row, group=None):
    """Evaluate `e` on a flattened row. For aggregate queries `group` is either
    the list of member rows or, for incrementally maintained views, a mapping
    from the aggregate node's id() to its already-maintained value."""
    if e.op == 'lit': return e.value
    if e.op == 'col': return row[e.index]
    if e.op in AGGREGATES:
        if isinstance(group, dict): return group[id(e)]
        if e.op == 'COUNT' and not e.args: return len(group)
        vs = [v for v in (evaluate(e.args[0], r) for r in group) if v is not None]
        if e.op == 'COUNT': return len(vs)
        if not vs: return None
        return {'SUM': sum, 'MIN': min, 'MAX': max}[e.op](vs)
    if e.op == 'COALESCE':
        for a in e.args:
            v = evaluate(a, row, group)
            if v is not None: return v
        return None
    a = evaluate(e.args[0], row, group)
    if e.op == 'ISNULL': return a is None
    if e.op == 'ISNOTNULL': return a is not None
    if e.op == 'NOT': return None if a is None else not a
    b = evaluate(e.args[1], row, group)
    if e.op == 'AND':
        if a is False or b is False: return False
        return None if a is None or b is None else True
    if e.op == 'OR':
        if a is True or b is True: return True
        return None if a is None or b is None else False
    if a is None or b is None: return None
    return SCALAR[e.op](a, b)


SCALAR = {
    '+': lambda a, b: a + b,
    '-': lambda a, b: a - b,
    '*': lambda a, b: a * b,
    '=': lambda a, b: a == b,
    '<>': lambda a, b: a != b,
    '<': lambda a, b: a < b,
    '<=': lambda a, b: a <= b,
    '>': lambda a, b: a > b,
    '>=': lambda a, b: a >= b,
}


def literal(value, type):
    return Expr('lit', value, type=type, nullable=value is None)


def same_column(a, b):
    return a.op == 'col' and b.op == 'col' and a.index == b.index


def fold(e):
    """Bottom-up constant folding that respects three-valued logic.

    Rewrites that are only valid for non-null operands (x = x, x IS NULL) use
    the binder's nullable flag, which already accounts for outer-join padding."""
    e.args = [fold(a) for a in e.args]
    if e.op in AGGREGATES | {'lit', 'col'}: return e
    if all(a.op == 'lit' for a in e.args):
        return literal(evaluate(e, []), e.type)
    if e.op in ('AND', 'OR'):
        absorbing, identity = (False, True) if e.op == 'AND' else (True, False)
        for a in e.args:
            if a.op == 'lit' and a.value is absorbing: return literal(absorbing, 'bool')
        for a, other in ((e.args[0], e.args[1]), (e.args[1], e.args[0])):
            if a.op == 'lit' and a.value is identity: return other
    elif e.op == 'ISNULL' and not e.args[0].nullable: return literal(False, 'bool')
    elif e.op == 'ISNOTNULL' and not e.args[0].nullable: return literal(True, 'bool')
    elif e.op in ('=', '<=', '>=') and same_column(*e.args) and not e.args[0].nullable: return literal(True, 'bool')
    elif e.op in ('<>', '<', '>') and same_column(*e.args) and not e.args[0].nullable: return literal(False, 'bool')
    elif e.op == 'COALESCE':
        args = []
        for a in e.args:
            if a.op == 'lit' and a.value is None: continue
            args.append(a)
            if not a.nullable: break
        if not args: return literal(None, e.type)
        if len(args) == 1: return args[0]
        e.args = args
    return e


def conjuncts(e):
    if e.op == 'AND': return conjuncts(e.args[0]) + conjuncts(e.args[1])
    return [e]


def null_rejecting(e, width):
    """True when the conjunct (referencing only `source`) cannot be TRUE on a
    row padded with NULL for that source, i.e. it discards outer-join padding."""
    return not is_true(evaluate(e, [None] * width))


def optimize(plan):
    q = plan.query
    q.select = [(fold(e), a) for e, a in q.select]
    q.joins = [fold(e) for e in q.joins]
    if q.having: q.having = fold(q.having)
    if q.where:
        width = sum(len(t['columns']) for t in plan.tables)
        remaining = []
        for e in conjuncts(fold(q.where)):
            if e.op == 'lit' and e.value is True: continue
            remaining.append(e)
        # A filter on a single source can run at that source's scan when every
        # output row carries that source's real values (it is never padded), or
        # when the filter rejects padded rows, in which case the LEFT JOIN that
        # pads it is equivalent to an INNER JOIN and can be converted first.
        changed = True
        while changed:
            changed, kept = False, []
            for e in remaining:
                sources = {n.source for n in walk(e) if n.op == 'col'}
                if len(plan.tables) > 1 and len(sources) == 1:
                    s = next(iter(sources))
                    padded = s > 0 and q.kinds[s - 1] == 'LEFT'
                    if padded and null_rejecting(e, width):
                        q.kinds[s - 1] = 'INNER'
                        padded = False
                    if not padded:
                        plan.filters[s].append(e)
                        changed = True
                        continue
                kept.append(e)
            remaining = kept
        q.where = None
        for e in remaining:
            q.where = e if q.where is None else Expr('AND', args=[q.where, e], type='bool')
    return plan


def sort_key(index, descending, nulls_first):
    # Python's sort is stable for reverse=True as well; the flag orders NULLs
    # relative to values and the tuple length difference avoids comparing None.
    null_rank = 0 if nulls_first != descending else 1
    return lambda r: (null_rank,) if r[index] is None else (1 - null_rank, r[index])


def execute(plan):
    q = plan.query
    scans, offset = [], 0
    for t, predicates in zip(plan.tables, plan.filters):
        scans.append([r for r in t['rows'] if all(is_true(evaluate(p, [None] * offset + r)) for p in predicates)])
        offset += len(t['columns'])
    rows = scans[0]
    for right, on, kind, t in zip(scans[1:], q.joins, q.kinds, plan.tables[1:]):
        padding = [None] * len(t['columns'])
        joined = []
        for l in rows:
            matches = [l + r for r in right if is_true(evaluate(on, l + r))]
            if not matches and kind == 'LEFT': matches = [l + padding]
            joined.extend(matches)
        rows = joined
    if q.where: rows = [r for r in rows if is_true(evaluate(q.where, r))]
    if plan.aggregate:
        groups = {}
        for row in rows:
            key = tuple(evaluate(e, row) for e in q.groups)
            groups.setdefault(key, []).append(row)
        if not q.groups and not rows: groups[()] = []
        units = [(g[0] if g else [], g) for g in groups.values()]
    else: units = [(r, None) for r in rows]
    projected = [[evaluate(e, r, g) for e, _ in q.select] for r, g in units if q.having is None or is_true(evaluate(q.having, r, g))]
    if q.distinct: projected = [list(r) for r in dict.fromkeys(tuple(r) for r in projected)]
    for i, descending, nulls_first in reversed(q.order): projected.sort(key=sort_key(i, descending, nulls_first), reverse=descending)
    projected = projected[q.offset: None if q.limit is None else q.offset + q.limit]
    return {'columns': [a for _, a in q.select], 'rows': projected}
