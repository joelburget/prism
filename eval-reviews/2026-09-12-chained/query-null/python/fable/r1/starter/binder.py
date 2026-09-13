"""Resolve all names and types before optimization or row evaluation."""
from model import Plan, require
from parser import AGGREGATES


def walk(e):
    yield e
    for a in e.args: yield from walk(a)


def bind(q, database):
    aliases = [a for _, a in q.select]
    require(len(set(aliases)) == len(aliases), 'DUPLICATE_ALIAS')
    require(len(set(a for _, a in q.sources)) == len(q.sources), 'DUPLICATE_ALIAS')
    tables, columns = [], []
    for si, (name, alias) in enumerate(q.sources):
        require(name in database, 'UNKNOWN_TABLE')
        t = database[name]
        tables.append(t)
        # The null-supplied side of a LEFT JOIN may be padded with NULL regardless of declaration.
        padded = si > 0 and q.kinds[si - 1] == 'LEFT'
        columns.extend((alias, c['name'], c['type'], si, c['nullable'] or padded) for c in t['columns'])
        if si: check(q.joins[si - 1], columns, False, False, True)
    for e in q.groups: check(e, columns, False)
    keys = [e.index for e in q.groups]
    require(len(set(keys)) == len(keys), 'INVALID_AGGREGATION')
    if q.where: check(q.where, columns, False, False, True)
    for e, _ in q.select: check(e, columns, True)
    if q.having: check(q.having, columns, True, False, True)
    roots = [e for e, _ in q.select] + ([q.having] if q.having else [])
    aggregate = bool(q.groups) or any(e.op in AGGREGATES for root in roots for e in walk(root))
    require(not q.having or aggregate, 'INVALID_AGGREGATION')
    if aggregate:
        for root in roots: grouped(root, keys)
    q.order = [(aliases.index(a) if a in aliases else -1, d, nf) for a, d, nf in q.order]
    require(all(i >= 0 for i, _, _ in q.order), 'UNKNOWN_COLUMN')
    return Plan(q, tables, [[] for _ in tables], aggregate)


def grouped(e, keys):
    if e.op in AGGREGATES: return
    require(e.op != 'col' or e.index in keys, 'INVALID_AGGREGATION')
    for a in e.args: grouped(a, keys)


def check(e, columns, allow, inside=False, predicate=False):
    """Resolve names and compute type plus a conservative nullable flag.

    'null' is the type of an untyped NULL literal; it unifies with any type
    required by its context."""
    if e.op == 'col':
        qualifier, name = e.value
        matches = [(i, c) for i, c in enumerate(columns) if c[1] == name and (not qualifier or c[0] == qualifier)]
        require(bool(matches), 'UNKNOWN_COLUMN')
        require(len(matches) == 1, 'AMBIGUOUS_COLUMN')
        e.index, c = matches[0]
        e.type, e.source, e.nullable = c[2], c[3], c[4]
    elif e.op == 'lit':
        e.nullable = e.value is None
    else:
        agg = e.op in AGGREGATES
        require(not agg or (allow and not inside), 'INVALID_AGGREGATION')
        for a in e.args: check(a, columns, allow, inside or agg)
        ts = [a.type for a in e.args]
        typed = [t for t in ts if t != 'null']
        e.nullable = any(a.nullable for a in e.args)
        if e.op == 'COUNT':
            e.type, e.nullable = 'int', False
        elif e.op in ('SUM', '+', '-', '*'):
            require(all(t == 'int' for t in typed), 'TYPE_ERROR')
            e.type = 'int'
            if e.op == 'SUM': e.nullable = True
        elif e.op in ('MIN', 'MAX'):
            require(ts[0] in ('int', 'text', 'null'), 'TYPE_ERROR')
            e.type, e.nullable = ts[0], True
        elif e.op in ('NOT', 'AND', 'OR'):
            require(all(t == 'bool' for t in typed), 'TYPE_ERROR')
            e.type = 'bool'
        elif e.op in ('ISNULL', 'ISNOTNULL'):
            e.type, e.nullable = 'bool', False
        elif e.op == 'COALESCE':
            require(all(t == typed[0] for t in typed), 'TYPE_ERROR')
            e.type = typed[0] if typed else 'null'
            e.nullable = all(a.nullable for a in e.args)
        else:
            require(all(t == typed[0] for t in typed) and (not typed or typed[0] != 'bool' or e.op in ('=', '<>')), 'TYPE_ERROR')
            e.type = 'bool'
    require(not predicate or e.type in ('bool', 'null'), 'TYPE_ERROR')
