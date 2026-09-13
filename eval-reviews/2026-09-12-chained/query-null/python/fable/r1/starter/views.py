"""Incrementally maintained views over mutable base tables (checkpoint two).

A view keeps the bound (optionally optimized) plan of checkpoint one and a
chain of `Stage`s, one per FROM source, mirroring the engine's scan/join
pipeline. Each stage owns the bag of tuples it currently produces, keyed by
the private row IDs of its sources (None marks LEFT JOIN padding), plus the
indexes needed to retract or extend that bag from a base-table delta:

* by_prefix / by_right: which output tuples a prefix or right row contributes to,
* matches: how many right rows match each prefix (drives padding),
* prefix_index / right_index: hash buckets on the ON clause's equi-join
  conjuncts, so a changed row is only tested against candidate partners.

Above the join, WHERE keeps only TRUE rows; a non-aggregate view stores one
projected row per surviving tuple, an aggregate view stores per-group counters
(COUNT/SUM) and multisets (MIN/MAX) plus each group's earliest encounter key.
DISTINCT, ORDER BY and LIMIT/OFFSET run over the maintained result on read
and the rendered rows are cached until the next relevant change.

A batch is validated against a private copy of the touched tables; only a
fully valid batch commits and is then propagated to the views whose sources
changed. Encounter order is a per-table sequence number that an update keeps."""
import re
from collections import Counter
from model import DomainError, require
from parser import Parser, AGGREGATES
from binder import bind, walk
from engine import optimize, evaluate, is_true, conjuncts, sort_key

VIEW_NAME = re.compile(r'[a-z][a-z0-9-]{0,39}')
TABLE_NAME = re.compile(r'[a-z_][a-z0-9_]*')
MAX_ID = 2147483647
BOUND = 1_000_000_000
MAX_CHANGES = 200
PYTHON_TYPES = {'int': int, 'text': str, 'bool': bool}


def bucket_add(index, key, item):
    index.setdefault(key, set()).add(item)


def bucket_discard(index, key, item):
    bucket = index.get(key)
    if bucket is not None:
        bucket.discard(item)
        if not bucket: del index[key]


def net(events):
    """Collapse an ordered list of ('-'|'+', tuple, flat) events into disjoint
    removed/added lists relative to the state before the batch. A tuple that
    ends up present with unchanged values is dropped from both lists."""
    records, order = {}, []
    for sign, t, flat in events:
        rec = records.get(t)
        if rec is None:
            rec = records[t] = [sign == '-', flat if sign == '-' else None, None]
            order.append(t)
        rec[2] = flat if sign == '+' else None
    removed, added = [], []
    for t in order:
        before, old, new = records[t]
        if before and new is not None and old == new: continue
        if before: removed.append((t, old))
        if new is not None: added.append((t, new))
    return removed, added


class Stage:
    """Scan of source `s` (s == 0) or its join onto the prefix tuples of stage s-1."""

    def __init__(self, s, table, offset, kind, on, filters):
        self.s, self.offset, self.width = s, offset, len(table['columns'])
        self.kind, self.on, self.filters = kind, on, filters
        self.pad = [None] * offset
        self.lkeys, self.rkeys = [], []
        if on is not None:
            for c in conjuncts(on):
                if c.op != '=': continue
                a, b = c.args
                sa = {n.source for n in walk(a) if n.op == 'col'}
                sb = {n.source for n in walk(b) if n.op == 'col'}
                if sa and sb == {s} and all(x < s for x in sa): self.lkeys.append(a); self.rkeys.append(b)
                elif sb and sa == {s} and all(x < s for x in sb): self.lkeys.append(b); self.rkeys.append(a)
        self.live = {}          # output tuple -> flat row
        self.by_prefix = {}     # prefix tuple -> set of output tuples
        self.by_right = {}      # right row id -> set of output tuples
        self.matches = {}       # prefix tuple -> number of TRUE matches
        self.prefix_key = {}    # prefix tuple -> equi-join key (None: can never match)
        self.prefix_index = {}  # key -> set of prefixes
        self.right_rows = {}    # right row id -> row (only rows passing scan filters)
        self.right_key = {}     # right row id -> key
        self.right_index = {}   # key -> set of right row ids

    def passes(self, row):
        return all(is_true(evaluate(p, self.pad + row)) for p in self.filters)

    @staticmethod
    def key_of(exprs, row):
        values = tuple(evaluate(e, row) for e in exprs)
        return None if any(v is None for v in values) else values

    def apply(self, prefix_events, deleted, inserted, prev_live):
        """Propagate a base delta for this source and the netted prefix delta
        from the previous stage; return this stage's netted output delta."""
        events = []
        if self.s == 0:
            for rid in deleted:
                t = (rid,)
                if t in self.live: events.append(('-', t, self.live.pop(t)))
            for rid, row in inserted.items():
                if self.passes(row):
                    self.live[(rid,)] = row
                    events.append(('+', (rid,), row))
            return net(events)

        removed_prefixes, added_prefixes = prefix_events
        padding = [None] * self.width

        def emit(t, flat):
            self.live[t] = flat
            events.append(('+', t, flat))
            bucket_add(self.by_prefix, t[:-1], t)
            if t[-1] is not None: bucket_add(self.by_right, t[-1], t)

        def retract(t):
            events.append(('-', t, self.live.pop(t)))
            bucket_discard(self.by_prefix, t[:-1], t)
            if t[-1] is not None: bucket_discard(self.by_right, t[-1], t)

        # Retractions first: prefixes that vanished, then right rows that vanished.
        for p, _ in removed_prefixes:
            for t in list(self.by_prefix.get(p, ())): retract(t)
            del self.matches[p]
            key = self.prefix_key.pop(p)
            if key is not None: bucket_discard(self.prefix_index, key, p)
        for rid in deleted:
            if rid not in self.right_rows: continue
            del self.right_rows[rid]
            key = self.right_key.pop(rid)
            if key is not None: bucket_discard(self.right_index, key, rid)
            for t in list(self.by_right.get(rid, ())):
                p = t[:-1]
                retract(t)
                self.matches[p] -= 1
                if self.matches[p] == 0 and self.kind == 'LEFT': emit(p + (None,), prev_live[p] + padding)
        # Additions: new right rows against surviving prefixes, then new prefixes
        # against the full right side, so every new pair is produced exactly once.
        for rid, row in inserted.items():
            if not self.passes(row): continue
            self.right_rows[rid] = row
            key = self.key_of(self.rkeys, self.pad + row)
            self.right_key[rid] = key
            if key is None: continue
            bucket_add(self.right_index, key, rid)
            for p in list(self.prefix_index.get(key, ())):
                flat = prev_live[p] + row
                if is_true(evaluate(self.on, flat)):
                    if self.matches[p] == 0 and self.kind == 'LEFT': retract(p + (None,))
                    self.matches[p] += 1
                    emit(p + (rid,), flat)
        for p, pflat in added_prefixes:
            key = self.key_of(self.lkeys, pflat)
            self.prefix_key[p] = key
            self.matches[p] = 0
            if key is not None:
                bucket_add(self.prefix_index, key, p)
                for rid in self.right_index.get(key, ()):
                    flat = pflat + self.right_rows[rid]
                    if is_true(evaluate(self.on, flat)):
                        self.matches[p] += 1
                        emit(p + (rid,), flat)
            if self.matches[p] == 0 and self.kind == 'LEFT': emit(p + (None,), pflat + padding)
        return net(events)


def agg_state(node):
    if node.op == 'COUNT': return [0]
    if node.op == 'SUM': return [0, 0]
    return [Counter(), None]


def agg_add(node, state, flat):
    if node.op == 'COUNT' and not node.args:
        state[0] += 1
        return
    v = evaluate(node.args[0], flat)
    if v is None: return
    if node.op == 'COUNT': state[0] += 1
    elif node.op == 'SUM':
        state[0] += v
        state[1] += 1
    else:
        values, best = state
        values[v] += 1
        if best is None or (v < best if node.op == 'MIN' else v > best): state[1] = v


def agg_remove(node, state, flat):
    if node.op == 'COUNT' and not node.args:
        state[0] -= 1
        return
    v = evaluate(node.args[0], flat)
    if v is None: return
    if node.op == 'COUNT': state[0] -= 1
    elif node.op == 'SUM':
        state[0] -= v
        state[1] -= 1
    else:
        values = state[0]
        values[v] -= 1
        if values[v] == 0:
            del values[v]
            # Only the loss of the current extreme revisits the group's remaining values.
            if v == state[1]: state[1] = (min(values) if node.op == 'MIN' else max(values)) if values else None


def agg_value(node, state):
    if node.op == 'COUNT': return state[0]
    if node.op == 'SUM': return state[0] if state[1] else None
    return state[1]


class Group:
    __slots__ = ('count', 'members', 'first', 'states', 'rep', 'output')

    def __init__(self, key, groups, width, aggs):
        self.count, self.members, self.first, self.output = 0, {}, None, None
        self.states = [agg_state(n) for n in aggs]
        # Non-aggregate references can only name group keys, so a row carrying
        # the key values in their slots represents the group for projection.
        self.rep = [None] * width
        for e, v in zip(groups, key): self.rep[e.index] = v


class View:
    def __init__(self, sql, optimized, database):
        plan = bind(Parser(sql).parse(), database)
        self.plan = optimize(plan) if optimized else plan
        q = self.plan.query
        self.sources = [t['name'] for t in self.plan.tables]
        self.stages, offset = [], 0
        for s, t in enumerate(self.plan.tables):
            self.stages.append(Stage(s, t, offset, q.kinds[s - 1] if s else None, q.joins[s - 1] if s else None, self.plan.filters[s]))
            offset += len(t['columns'])
        self.width = offset
        roots = [e for e, _ in q.select] + ([q.having] if q.having else [])
        self.aggs = [n for root in roots for n in walk(root) if n.op in AGGREGATES]
        self.units = {}     # non-aggregate: tuple -> (encounter key, projected row)
        self.groups = {}    # aggregate: group key -> Group
        self.dirty = set()
        self.rendered = None
        if self.plan.aggregate and not q.groups:
            self.groups[()] = Group((), [], self.width, self.aggs)   # the global group exists even when empty
            self.dirty.add(())
        self.apply({name: ({}, database[name]['live']) for name in set(self.sources)}, database)

    def apply(self, deltas, database):
        """deltas: table name -> (deleted {id: old row}, inserted {id: new row})."""
        events, prev = None, None
        for stage in self.stages:
            deleted, inserted = deltas.get(self.sources[stage.s], ({}, {}))
            events = stage.apply(events, deleted, inserted, prev)
            prev = stage.live
        removed, added = events
        where = self.plan.query.where
        for t, flat in removed:
            if where is None or is_true(evaluate(where, flat)): self.retract(t, flat)
        for t, flat in added:
            if where is None or is_true(evaluate(where, flat)):
                self.insert(t, flat, tuple(-1 if rid is None else database[name]['used'][rid] for name, rid in zip(self.sources, t)))
        self.rendered = None

    def insert(self, t, flat, okey):
        q = self.plan.query
        if not self.plan.aggregate:
            self.units[t] = (okey, [evaluate(e, flat) for e, _ in q.select])
            return
        key = tuple(evaluate(e, flat) for e in q.groups)
        g = self.groups.get(key)
        if g is None: g = self.groups[key] = Group(key, q.groups, self.width, self.aggs)
        g.count += 1
        g.members[t] = okey
        if g.first is None or okey < g.first: g.first = okey
        for n, st in zip(self.aggs, g.states): agg_add(n, st, flat)
        self.dirty.add(key)

    def retract(self, t, flat):
        q = self.plan.query
        if not self.plan.aggregate:
            del self.units[t]
            return
        key = tuple(evaluate(e, flat) for e in q.groups)
        g = self.groups[key]
        g.count -= 1
        okey = g.members.pop(t)
        for n, st in zip(self.aggs, g.states): agg_remove(n, st, flat)
        if g.count == 0 and q.groups:
            del self.groups[key]
            self.dirty.discard(key)
            return
        if okey == g.first: g.first = min(g.members.values(), default=None)
        self.dirty.add(key)

    def render_group(self, g):
        q = self.plan.query
        values = {id(n): agg_value(n, st) for n, st in zip(self.aggs, g.states)}
        if q.having is not None and not is_true(evaluate(q.having, g.rep, values)): g.output = None
        else: g.output = [evaluate(e, g.rep, values) for e, _ in q.select]

    def rows(self):
        if self.rendered is not None: return self.rendered
        q = self.plan.query
        if self.plan.aggregate:
            for key in self.dirty: self.render_group(self.groups[key])
            self.dirty.clear()
            ordered = sorted(self.groups.values(), key=lambda g: g.first if g.first is not None else ())
            projected = [g.output for g in ordered if g.output is not None]
        else:
            projected = [row for _, row in sorted(self.units.values(), key=lambda u: u[0])]
        if q.distinct: projected = [list(r) for r in dict.fromkeys(tuple(r) for r in projected)]
        else: projected = list(projected)
        for i, descending, nulls_first in reversed(q.order): projected.sort(key=sort_key(i, descending, nulls_first), reverse=descending)
        self.rendered = projected[q.offset: None if q.limit is None else q.offset + q.limit]
        return self.rendered


def shape(value, expected, code='INVALID_COMMAND'):
    require(isinstance(value, dict) and set(value) == expected, code)


def valid_id(v):
    return type(v) is int and 1 <= v <= MAX_ID


class Store:
    """Mutable database plus named views; one instance per request."""

    def __init__(self, database):
        self.database = database
        for t in database.values():
            t['live'] = {i + 1: row for i, row in enumerate(t['rows'])}   # id -> row, encounter order
            t['used'] = {i + 1: i for i in range(len(t['rows']))}         # id -> sequence number
            t['next_seq'] = len(t['rows'])
        self.views = {}
        self.revision = 0

    def run(self, command):
        try:
            return {'ok': True, 'result': self.execute(command)}
        except DomainError as e:
            return {'ok': False, 'error': {'code': str(e)}}
        except (ValueError, KeyError, TypeError, AttributeError):
            return {'ok': False, 'error': {'code': 'INVALID_COMMAND'}}

    def execute(self, command):
        require(isinstance(command, dict) and isinstance(command.get('op'), str), 'INVALID_COMMAND')
        op = command['op']
        if op == 'apply':
            shape(command, {'op', 'changes'})
            return self.apply(command['changes'])
        if op == 'create':
            shape(command, {'op', 'view', 'sql', 'optimize'})
            name = command['view']
            require(isinstance(name, str) and VIEW_NAME.fullmatch(name) and isinstance(command['sql'], str) and type(command['optimize']) is bool, 'INVALID_COMMAND')
            require(name not in self.views, 'VIEW_EXISTS')
            self.views[name] = View(command['sql'], command['optimize'], self.database)
            return {'view': name, 'revision': self.revision}
        shape(command, {'op', 'view'})
        name = command['view']
        require(isinstance(name, str) and VIEW_NAME.fullmatch(name) and op in ('read', 'drop'), 'INVALID_COMMAND')
        require(name in self.views, 'UNKNOWN_VIEW')
        if op == 'drop':
            del self.views[name]
            return {'dropped': name}
        return {'revision': self.revision, 'columns': [a for _, a in self.views[name].plan.query.select], 'rows': [list(r) for r in self.views[name].rows()]}

    def apply(self, changes):
        require(isinstance(changes, list) and 0 < len(changes) <= MAX_CHANGES, 'INVALID_COMMAND')
        for c in changes:
            require(isinstance(c, dict) and c.get('op') in ('insert', 'update', 'delete'), 'INVALID_COMMAND')
            shape(c, {'op', 'table', 'id'} | ({'row'} if c['op'] != 'delete' else set()))
            require(isinstance(c['table'], str) and TABLE_NAME.fullmatch(c['table']) and valid_id(c['id']), 'INVALID_COMMAND')
            require(c['op'] == 'delete' or isinstance(c['row'], list), 'INVALID_COMMAND')
        # Private batch state: copies of the touched tables' live rows and ID sets.
        work = {}
        for c in changes:
            name = c['table']
            require(name in self.database, 'UNKNOWN_TABLE')
            t = self.database[name]
            w = work.get(name)
            if w is None: w = work[name] = {'live': dict(t['live']), 'used': dict(t['used']), 'next_seq': t['next_seq']}
            rid = c['id']
            if c['op'] != 'delete':
                row = c['row']
                require(len(row) == len(t['columns']), 'INVALID_ROW')
                for v, col in zip(row, t['columns']):
                    if v is None: require(col['nullable'], 'INVALID_ROW')
                    else:
                        require(type(v) is PYTHON_TYPES[col['type']], 'INVALID_ROW')
                        require(col['type'] != 'int' or -BOUND <= v <= BOUND, 'INVALID_ROW')
                row = list(row)
            if c['op'] == 'insert':
                require(rid not in w['used'], 'ROW_ID_USED')
                w['used'][rid] = w['next_seq']
                w['next_seq'] += 1
                w['live'][rid] = row
            else:
                require(rid in w['live'], 'UNKNOWN_ROW')
                if c['op'] == 'update': w['live'][rid] = row    # reassignment keeps the encounter position
                else: del w['live'][rid]
        # Commit: swap in the table copies, then propagate the net delta of each table.
        deltas = {}
        for name, w in work.items():
            t = self.database[name]
            old, new = t['live'], w['live']
            deleted = {rid: row for rid, row in old.items() if rid not in new or new[rid] != row}
            inserted = {rid: row for rid, row in new.items() if rid not in old or old[rid] != row}
            t['live'], t['used'], t['next_seq'] = new, w['used'], w['next_seq']
            if deleted or inserted: deltas[name] = (deleted, inserted)
        for view in self.views.values():
            touched = {name: deltas[name] for name in view.sources if name in deltas}
            if touched: view.apply(touched, self.database)
        self.revision += 1
        return {'revision': self.revision}
