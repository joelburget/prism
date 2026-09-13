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
        padded = si > 0 and q.join_kinds[si - 1] == 'LEFT'
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
    q.order = [(aliases.index(a) if a in aliases else -1, d, n) for a, d, n in q.order]
    require(all(i >= 0 for i, _, _ in q.order), 'UNKNOWN_COLUMN')
    return Plan(q, tables, [[] for _ in tables], aggregate)


def grouped(e, keys):
    if e.op in AGGREGATES: return
    require(e.op != 'col' or e.index in keys, 'INVALID_AGGREGATION')
    for a in e.args: grouped(a, keys)


ALL_TYPES = {'int', 'text', 'bool'}


def constrain(e, types):
    """Unify polymorphic NULLs, including those inside COALESCE and MIN/MAX."""
    e.types &= types
    require(bool(e.types), 'TYPE_ERROR')
    e.type = next(iter(e.types)) if len(e.types) == 1 else 'null'
    if e.op in ('COALESCE', 'MIN', 'MAX'):
        for a in e.args: constrain(a, e.types)


def check(e, columns, allow, inside=False, predicate=False):
    if e.op == 'col':
        qualifier, name = e.value
        matches = [(i, c) for i, c in enumerate(columns) if c[1] == name and (not qualifier or c[0] == qualifier)]
        require(bool(matches), 'UNKNOWN_COLUMN')
        require(len(matches) == 1, 'AMBIGUOUS_COLUMN')
        e.index, c = matches[0]
        e.type, e.source = c[2], c[3]
        e.types, e.nullable = {e.type}, c[4]
    elif e.op == 'lit':
        e.types = ALL_TYPES.copy() if e.value is None else {e.type}
        e.nullable = e.value is None
    else:
        agg = e.op in AGGREGATES
        require(not agg or (allow and not inside), 'INVALID_AGGREGATION')
        for a in e.args: check(a, columns, allow, inside or agg)
        e.nullable = any(a.nullable for a in e.args)
        if e.op == 'COUNT':
            e.types, e.nullable = {'int'}, False
        elif e.op in ('SUM', '+', '-', '*'):
            for a in e.args: constrain(a, {'int'})
            e.types = {'int'}
            if e.op == 'SUM': e.nullable = True
        elif e.op in ('MIN', 'MAX', 'COALESCE'):
            common = ALL_TYPES.copy() if e.op == 'COALESCE' else {'int', 'text'}
            for a in e.args: common &= a.types
            for a in e.args: constrain(a, common)
            e.types = common
            e.nullable = all(a.nullable for a in e.args) if e.op == 'COALESCE' else True
        elif e.op in ('NOT', 'AND', 'OR'):
            for a in e.args: constrain(a, {'bool'})
            e.types = {'bool'}
        elif e.op in ('IS NULL', 'IS NOT NULL'):
            e.types, e.nullable = {'bool'}, False
        else:
            common = e.args[0].types & e.args[1].types
            if e.op not in ('=', '<>'): common &= {'int', 'text'}
            for a in e.args: constrain(a, common)
            e.types = {'bool'}
    constrain(e, {'bool'} if predicate else ALL_TYPES)
