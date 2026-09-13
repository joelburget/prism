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
        columns.extend((alias, c['name'], c['type'], si) for c in t['columns'])
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
    if not q.groups:
        require(not any(e.op in {'SUM', 'MIN', 'MAX'} for root in roots for e in walk(root)), 'UNSUPPORTED_FEATURE')
    q.order = [(aliases.index(a) if a in aliases else -1, d) for a, d in q.order]
    require(all(i >= 0 for i, _ in q.order), 'UNKNOWN_COLUMN')
    return Plan(q, tables, [[] for _ in tables], aggregate)


def grouped(e, keys):
    if e.op in AGGREGATES: return
    require(e.op != 'col' or e.index in keys, 'INVALID_AGGREGATION')
    for a in e.args: grouped(a, keys)


def check(e, columns, allow, inside=False, predicate=False):
    if e.op == 'col':
        qualifier, name = e.value
        matches = [(i, c) for i, c in enumerate(columns) if c[1] == name and (not qualifier or c[0] == qualifier)]
        require(bool(matches), 'UNKNOWN_COLUMN')
        require(len(matches) == 1, 'AMBIGUOUS_COLUMN')
        e.index, c = matches[0]
        e.type, e.source = c[2], c[3]
    elif e.op != 'lit':
        agg = e.op in AGGREGATES
        require(not agg or (allow and not inside), 'INVALID_AGGREGATION')
        for a in e.args: check(a, columns, allow, inside or agg)
        ts = [a.type for a in e.args]
        if e.op == 'COUNT': e.type = 'int'
        elif e.op in ('SUM', '+', '-', '*'):
            require(all(t == 'int' for t in ts), 'TYPE_ERROR')
            e.type = 'int'
        elif e.op in ('MIN', 'MAX'):
            require(ts[0] in ('int', 'text'), 'TYPE_ERROR')
            e.type = ts[0]
        elif e.op in ('NOT', 'AND', 'OR'):
            require(all(t == 'bool' for t in ts), 'TYPE_ERROR')
            e.type = 'bool'
        else:
            require(ts[0] == ts[1] and (ts[0] != 'bool' or e.op in ('=', '<>')), 'TYPE_ERROR')
            e.type = 'bool'
    require(not predicate or e.type == 'bool', 'TYPE_ERROR')
