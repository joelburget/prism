"""Incremental left-deep joins and retractable aggregate contributions.

Lineage keys contain monotonically assigned encounter tokens (zero denotes
outer-join padding). Each join retains indexes on equality conjuncts and its
children per left lineage. Deltas propagate through stages against the final
batch state, so simultaneous changes on both sides are processed once.
"""
import re
import heapq
from collections import Counter
from model import require, DomainError
from parser import Parser, AGGREGATES
from binder import bind, walk
from engine import evaluate, optimize, conjuncts


class Join:
    def __init__(self, on, kind, offset, width):
        self.on, self.kind, self.offset, self.width = on, kind, offset, width
        self.pairs = []
        for e in conjuncts(on):
            if e.op == '=' and all(a.op == 'col' for a in e.args):
                a, b = sorted(a.index for a in e.args)
                if a < offset <= b:
                    self.pairs.append((a, b - offset))
        self.left, self.right, self.li, self.ri, self.children = {}, {}, {}, {}, {}

    def key(self, row, side):
        return tuple(row[p[side]] for p in self.pairs)

    def change(self, rows, index, delta, side):
        for token, row in delta.items():
            old = rows.pop(token, None)
            if old is not None:
                k = self.key(old, side)
                index[k].remove(token)
                if not index[k]: del index[k]
            if row is not None:
                rows[token] = row
                index.setdefault(self.key(row, side), set()).add(token)

    def update(self, ld, rd):
        affected = set(ld)
        for token, row in rd.items():
            for r in (self.right.get(token), row):
                if r is not None:
                    affected.update(self.li.get(self.key(r, 1), ()))
        self.change(self.left, self.li, ld, 0)
        self.change(self.right, self.ri, rd, 1)
        out = {}
        for token in affected:
            old = self.children.pop(token, {})
            new = {}
            if token in self.left:
                left = self.left[token]
                for rt in self.ri.get(self.key(left, 0), ()):
                    row = left + self.right[rt]
                    if evaluate(self.on, row) is True: new[token + rt] = row
                if not new and self.kind == 'LEFT':
                    new[token + (0,)] = left + [None] * self.width
                self.children[token] = new
            for k in old.keys() | new.keys():
                if old.get(k) != new.get(k): out[k] = new.get(k)
        return out


class Group:
    def __init__(self, aggregates):
        self.rows = {}
        self.order = []
        self.aggregates = aggregates
        self.values = {id(e): Counter() for e in aggregates}
        self.counts = {id(e): 0 for e in aggregates}
        self.sums = {id(e): 0 for e in aggregates if e.op == 'SUM'}

    def change(self, token, row, sign):
        if sign == 1:
            self.rows[token] = row
            heapq.heappush(self.order, token)
        else: del self.rows[token]
        if len(self.order) > 2 * len(self.rows) + 64:
            self.order = list(self.rows)
            heapq.heapify(self.order)
        for e in self.aggregates:
            if not e.args: continue
            v = evaluate(e.args[0], row)
            if v is None: continue
            self.counts[id(e)] += sign
            counts = self.values[id(e)]
            counts[v] += sign
            if not counts[v]: del counts[v]
            if e.op == 'SUM': self.sums[id(e)] += sign * v

    def first(self):
        while self.order and self.order[0] not in self.rows:
            heapq.heappop(self.order)
        return self.order[0] if self.order else ()

    def aggregate(self, e):
        if not e.args: return len(self.rows)
        vs = self.values[id(e)]
        if e.op == 'COUNT': return self.counts[id(e)]
        if not vs: return None
        if e.op == 'SUM': return self.sums[id(e)]
        return (min if e.op == 'MIN' else max)(vs)


class View:
    def __init__(self, plan, stores):
        self.plan, self.q = plan, plan.query
        self.names = [n for n, _ in self.q.sources]
        self.joins, self.offsets = [], []
        offset = 0
        for i, t in enumerate(plan.tables):
            self.offsets.append(offset)
            if i: self.joins.append(Join(self.q.joins[i-1], self.q.join_kinds[i-1], offset, len(t['columns'])))
            offset += len(t['columns'])
        self.rows, self.groups, self.projected = {}, {}, {}
        roots = [e for e, _ in self.q.select] + ([self.q.having] if self.q.having else [])
        self.aggregates = [e for root in roots for e in walk(root) if e.op in AGGREGATES]
        self.update({n: {token: row for token, row in stores[n].rows.values()} for n in set(self.names)})

    def update(self, changes):
        if not set(self.names).intersection(changes): return
        scans = []
        for i, name in enumerate(self.names):
            delta = {}
            for token, row in changes.get(name, {}).items():
                if row is not None and not all(evaluate(p, [None]*self.offsets[i] + row) is True for p in self.plan.filters[i]): row = None
                delta[(token,)] = row
            scans.append(delta)
        delta = scans[0]
        for stage, right in zip(self.joins, scans[1:]): delta = stage.update(delta, right)
        touched = set()
        for token, row in delta.items():
            old = self.rows.pop(token, None)
            if row is not None and self.q.where and evaluate(self.q.where, row) is not True: row = None
            if self.plan.aggregate:
                for r, sign in ((old, -1), (row, 1)):
                    if r is None: continue
                    key = tuple(evaluate(e, r) for e in self.q.groups)
                    if key not in self.groups: self.groups[key] = Group(self.aggregates)
                    group = self.groups[key]
                    group.change(token, r, sign)
                    touched.add(key)
            else:
                self.projected.pop(token, None)
                if row is not None: self.projected[token] = [evaluate(e, row) for e, _ in self.q.select]
            if row is not None: self.rows[token] = row
        if self.plan.aggregate:
            if not self.q.groups:
                self.groups.setdefault((), Group(self.aggregates))
                touched.add(())
            for key in touched:
                self.projected.pop(key, None)
                g = self.groups[key]
                if not g.rows and self.q.groups:
                    del self.groups[key]
                    continue
                first = g.first()
                row = g.rows[first] if g.rows else []
                if self.q.having is None or evaluate(self.q.having, row, g) is True:
                    self.projected[key] = (first, [evaluate(e, row, g) for e, _ in self.q.select])
        self.render()

    def render(self):
        if self.plan.aggregate: rows = [r for _, r in sorted(self.projected.values())]
        else: rows = [self.projected[k] for k in sorted(self.projected)]
        if self.q.distinct: rows = [list(r) for r in dict.fromkeys(tuple(r) for r in rows)]
        for i, descending, nulls_first in reversed(self.q.order):
            rows.sort(key=lambda r: (r[i] is not None, r[i]), reverse=descending)
            rows.sort(key=lambda r: (r[i] is not None) if nulls_first else (r[i] is None))
        self.result = {'columns': [a for _, a in self.q.select], 'rows': rows[self.q.offset:None if self.q.limit is None else self.q.offset+self.q.limit]}


class Store:
    def __init__(self, table):
        self.table = table
        self.rows = {i: (i, row) for i, row in enumerate(table['rows'], 1)}
        self.used = set(self.rows)
        self.next = len(self.rows) + 1


def shape(obj, keys):
    require(isinstance(obj, dict) and set(obj) == set(keys.split()), 'INVALID_COMMAND')


def name(value, pattern):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def valid_row(row, table):
    require(len(row) == len(table['columns']), 'INVALID_ROW')
    for v, c in zip(row, table['columns']):
        require(c['nullable'] if v is None else type(v) is {'int': int, 'text': str, 'bool': bool}[c['type']], 'INVALID_ROW')
        if type(v) is int: require(-1000000000 <= v <= 1000000000, 'INVALID_ROW')


def commands(database, commands):
    stores = {n: Store(t) for n, t in database.items()}
    views, replies, revision = {}, [], 0
    for cmd in commands:
        try:
            require(isinstance(cmd, dict) and isinstance(cmd.get('op'), str), 'INVALID_COMMAND')
            op = cmd['op']
            require(op in ('create', 'read', 'drop', 'apply'), 'INVALID_COMMAND')
            if op == 'apply':
                shape(cmd, 'op changes')
                changes = cmd['changes']
                require(isinstance(changes, list) and 1 <= len(changes) <= 200, 'INVALID_COMMAND')
                for c in changes:
                    require(isinstance(c, dict) and isinstance(c.get('op'), str) and c['op'] in ('insert', 'update', 'delete'), 'INVALID_COMMAND')
                    shape(c, 'op table id' if c['op'] == 'delete' else 'op table id row')
                    require(name(c['table'], '[a-z_][a-z0-9_]*') and type(c['id']) is int and 1 <= c['id'] <= 2147483647, 'INVALID_COMMAND')
                    if c['op'] != 'delete': require(isinstance(c['row'], list), 'INVALID_COMMAND')
                pending, inserted, nexts = {}, {}, {}
                for c in changes:
                    n, rid, action = c['table'], c['id'], c['op']
                    require(n in stores, 'UNKNOWN_TABLE')
                    s = stores[n]
                    if action != 'delete': valid_row(c['row'], s.table)
                    overlay = pending.setdefault(n, {})
                    used = inserted.setdefault(n, set())
                    current = overlay.get(rid, s.rows.get(rid))
                    if action == 'insert':
                        require(rid not in s.used and rid not in used, 'ROW_ID_USED')
                        token = nexts.get(n, s.next)
                        nexts[n] = token + 1
                        used.add(rid)
                    else:
                        require(current is not None, 'UNKNOWN_ROW')
                        token = current[0]
                    overlay[rid] = None if action == 'delete' else (token, c['row'])
                deltas = {}
                for n, overlay in pending.items():
                    s, delta = stores[n], {}
                    for rid, new in overlay.items():
                        old = s.rows.get(rid)
                        if old == new: continue
                        if old is not None: delta[old[0]] = None
                        if new is None: s.rows.pop(rid, None)
                        else:
                            s.rows[rid] = new
                            delta[new[0]] = new[1]
                    s.used.update(inserted[n])
                    s.next = nexts.get(n, s.next)
                    if delta: deltas[n] = delta
                for view in views.values(): view.update(deltas)
                revision += 1
                result = {'revision': revision}
            else:
                shape(cmd, 'op view sql optimize' if op == 'create' else 'op view')
                v = cmd['view']
                require(name(v, '[a-z][a-z0-9-]{0,39}'), 'INVALID_COMMAND')
                if op == 'create':
                    require(isinstance(cmd['sql'], str) and type(cmd['optimize']) is bool, 'INVALID_COMMAND')
                    require(v not in views, 'VIEW_EXISTS')
                    plan = bind(Parser(cmd['sql']).parse(), database)
                    views[v] = View(optimize(plan) if cmd['optimize'] else plan, stores)
                    result = {'view': v, 'revision': revision}
                else:
                    require(v in views, 'UNKNOWN_VIEW')
                    if op == 'drop':
                        del views[v]
                        result = {'dropped': v}
                    else: result = dict(views[v].result, revision=revision)
            replies.append({'ok': True, 'result': result})
        except DomainError as e:
            replies.append({'ok': False, 'error': {'code': str(e)}})
    return replies
