"""Stable relational execution and semantics-preserving baseline optimization."""
from model import Expr
from parser import AGGREGATES
from binder import walk


def evaluate(e, row, group=None):
    if e.op == 'lit': return e.value
    if e.op == 'col': return row[e.index]
    if e.op in AGGREGATES:
        if e.op == 'COUNT': 
            if len(e.args) == 0:
                return len(group)
            else:
                return sum(1 for r in group if evaluate(e.args[0], r) is not None)
        vs = [evaluate(e.args[0], r) for r in group if evaluate(e.args[0], r) is not None]
        if not vs: return None
        return {'SUM': sum, 'MIN': min, 'MAX': max}[e.op](vs)
    if e.op == 'IS NULL':
        return evaluate(e.args[0], row, group) is None
    if e.op == 'IS NOT NULL':
        return evaluate(e.args[0], row, group) is not None
    if e.op == 'COALESCE':
        for a in e.args:
            v = evaluate(a, row, group)
            if v is not None:
                return v
        return None
    a = evaluate(e.args[0], row, group)
    if e.op == 'NOT': 
        if a is None: return None
        return not a
    b = evaluate(e.args[1], row, group)
    if e.op == '+': 
        if a is None or b is None: return None
        return a + b
    if e.op == '-': 
        if a is None or b is None: return None
        return a - b
    if e.op == '*': 
        if a is None or b is None: return None
        return a * b
    if e.op == 'AND': 
        if a is False or b is False: return False
        if a is None or b is None: return None
        return a and b
    if e.op == 'OR': 
        if a is True or b is True: return True
        if a is None or b is None: return None
        return a or b
    if e.op == '=': 
        if a is None or b is None: return None
        return a == b
    if e.op == '<>': 
        if a is None or b is None: return None
        return a != b
    if e.op == '<': 
        if a is None or b is None: return None
        return a < b
    if e.op == '<=': 
        if a is None or b is None: return None
        return a <= b
    if e.op == '>': 
        if a is None or b is None: return None
        return a > b
    if e.op == '>=': 
        if a is None or b is None: return None
        return a >= b
    raise AssertionError(e.op)


def fold(e):
    e.args = [fold(a) for a in e.args]
    if e.op not in AGGREGATES | {'lit', 'col'} and all(a.op == 'lit' for a in e.args):
        result = evaluate(e, [])
        return Expr('lit', result, type=e.type)
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
                source_id = next(iter(sources))
                is_left_join = source_id > 0 and source_id - 1 < len(q.join_types) and q.join_types[source_id - 1]
                if not is_left_join:
                    plan.filters[source_id].append(e)
                    continue
            remaining.append(e)
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
    for right_idx, (right, on) in enumerate(zip(scans[1:], q.joins), 1):
        is_left = right_idx - 1 < len(q.join_types) and q.join_types[right_idx - 1]
        left_cols = sum(len(plan.tables[i]['columns']) for i in range(right_idx))
        right_cols = len(plan.tables[right_idx]['columns'])
        new_rows = []
        for l in rows:
            matched = False
            for r in right:
                combined = l + r
                on_result = evaluate(on, combined)
                if on_result is True:
                    new_rows.append(combined)
                    matched = True
            if is_left and not matched:
                padding = [None] * right_cols
                new_rows.append(l + padding)
        rows = new_rows
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
    if q.distinct:
        seen = []
        for r in projected:
            if r not in seen:
                seen.append(r)
        projected = seen
    
    # Handle ORDER BY with proper NULL handling
    # Apply sorts in reverse order (lowest priority to highest)
    for col_idx, desc, nulls_first in reversed(q.order):
        # Separate NULLs from non-NULLs
        nulls = [r for r in projected if r[col_idx] is None]
        non_nulls = [r for r in projected if r[col_idx] is not None]
        
        # Sort non-NULLs by the column value
        non_nulls.sort(key=lambda r: r[col_idx], reverse=desc)
        
        # Determine if NULLs should come first
        if nulls_first is None:
            # Default: LAST for ASC, FIRST for DESC
            null_comes_first = desc
        else:
            null_comes_first = nulls_first
        
        # Combine based on null positioning
        if null_comes_first:
            projected = nulls + non_nulls
        else:
            projected = non_nulls + nulls
    
    projected = projected[q.offset: None if q.limit is None else q.offset + q.limit]
    return {'columns': [a for _, a in q.select], 'rows': projected}
