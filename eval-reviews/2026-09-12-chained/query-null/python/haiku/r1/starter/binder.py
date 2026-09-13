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
        columns.extend((alias, c['name'], c['type'], si, c.get('nullable', False)) for c in t['columns'])
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
    plan = Plan(q, tables, [[] for _ in tables], aggregate)
    
    # Track which columns are made nullable by LEFT JOINs
    nullability = [[] for _ in tables]
    for si in range(1, len(tables)):
        if si - 1 < len(q.join_types) and q.join_types[si - 1]:  # LEFT JOIN
            nullability[si] = list(range(len(tables[si]['columns'])))
    plan.nullability = nullability
    
    return plan


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
        e.nullable = c[4]
    elif e.op == 'lit':
        if e.value is None:
            e.type = ''
            e.nullable = True
        elif e.type == '':
            e.nullable = False
    elif e.op == 'IS NULL' or e.op == 'IS NOT NULL':
        check(e.args[0], columns, allow, inside)
        e.type = 'bool'
        e.nullable = False
    elif e.op == 'COALESCE':
        arg_types = []
        all_nullable = True
        for a in e.args:
            check(a, columns, allow, inside or False)
            if a.type != '':
                arg_types.append(a.type)
            if not a.nullable:
                all_nullable = False
        require(len(set(arg_types)) <= 1, 'TYPE_ERROR')
        if arg_types:
            e.type = arg_types[0]
        else:
            e.type = ''
        e.nullable = all_nullable
    elif e.op != 'lit':
        agg = e.op in AGGREGATES
        require(not agg or (allow and not inside), 'INVALID_AGGREGATION')
        for a in e.args: check(a, columns, allow, inside or agg)
        ts = [a.type for a in e.args]
        if e.op == 'COUNT': 
            e.type = 'int'
            e.nullable = False
        elif e.op in ('SUM', '+', '-', '*'):
            require(all(t in ('int', '') for t in ts), 'TYPE_ERROR')
            e.type = 'int'
            e.nullable = any(a.nullable for a in e.args)
        elif e.op in ('MIN', 'MAX'):
            require(all(t in ('int', 'text', '') for t in ts), 'TYPE_ERROR')
            e.type = next((t for t in ts if t != ''), '')
            e.nullable = any(a.nullable for a in e.args)
        elif e.op in ('NOT', 'AND', 'OR'):
            require(all(t in ('bool', '') for t in ts), 'TYPE_ERROR')
            e.type = 'bool'
            if e.op == 'NOT':
                e.nullable = e.args[0].nullable
            else:
                e.nullable = any(a.nullable for a in e.args)
        else:
            require(all(t == '' or ts[0] == '' or t == ts[0] for t in ts[1:]) and (all(t != 'bool' for t in ts) or e.op in ('=', '<>')), 'TYPE_ERROR')
            e.type = 'bool'
            e.nullable = any(a.nullable for a in e.args)
    require(not predicate or e.type in ('bool', ''), 'TYPE_ERROR')
