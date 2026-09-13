"""Incremental maintenance of a bound query plan as a live view.

A view is the same `Plan` the batch engine executes, wired as a small dataflow
of operators that consume row deltas instead of whole relations.  Every row
carries a *provenance key*: the tuple of per-source sequence numbers it was
built from, with a zero standing for the NULL-padded side of a left join.
Sequence numbers are assigned per table when a row is first inserted and are
preserved by updates, so the lexicographic order of provenance keys is exactly
the nested-loop encounter order the contract prescribes.  That lets every
operator work on an unordered dict keyed by provenance while reads still
reproduce the deterministic order, and it makes deletion a keyed retraction
rather than a re-scan.
"""
from engine import Slots, evaluate, finish, true
from parser import AGGREGATES
from binder import walk

PAD = 0  # provenance component of a NULL-padded left-join right side


def sources_of(e):
    return {n.source for n in walk(e) if n.op == 'col'}


def conjuncts(e):
    if e is not None and e.op == 'AND':
        return conjuncts(e.args[0]) + conjuncts(e.args[1])
    return [] if e is None else [e]


class Source:
    """A base table as seen by one FROM item, after pushed scan predicates."""

    def __init__(self, offset, filters):
        self.offset = offset
        self.filters = filters
        self.rows = {}
        self.down = None
        self.side = None

    def keeps(self, row):
        pad = [None] * self.offset
        return all(true(evaluate(p, pad + row)) for p in self.filters)

    def add(self, seq, row):
        if not self.keeps(row): return
        key = (seq,)
        self.rows[key] = row
        self.down.add(key, row, self.side)

    def remove(self, seq, row):
        key = (seq,)
        if key not in self.rows: return  # the row was rejected by a pushed filter
        row = self.rows.pop(key)
        self.down.remove(key, row, self.side)


class JoinNode:
    """One incremental nested-loop join step with an optional equi-key index.

    Both directions are symmetric: a new row on either side probes the opposite
    side and emits the matching pairs, and a retraction drops exactly the output
    rows recorded for that side.  A left join additionally keeps a per-left-row
    match count so that losing the final match materialises the padded row and
    gaining the first match retracts it.
    """

    def __init__(self, source, left, right, on, outer, offset, width):
        self.source = source
        self.left, self.right = left, right
        self.on, self.outer = on, outer
        self.offset, self.width = offset, width
        self.rows = {}
        self.by_left = {}
        self.by_right = {}
        self.matches = {}
        self.down = None
        self.side = None
        self.left_keys, self.right_keys = self.equi_keys()
        self.lindex, self.rindex = {}, {}

    def equi_keys(self):
        """Equalities of `left expression = right expression` usable as a key.

        The ON clause is a conjunction, so it can only be TRUE when each such
        equality is TRUE, which in turn requires two equal non-NULL values.
        Probing by that key therefore never loses a match, and the full ON
        expression is still evaluated on every candidate pair.
        """
        left, right = [], []
        for c in conjuncts(self.on):
            if c.op != '=': continue
            a, b = c.args
            sa, sb = sources_of(a), sources_of(b)
            if sa and sb == {self.source} and max(sa) < self.source: left.append(a); right.append(b)
            elif sb and sa == {self.source} and max(sb) < self.source: left.append(b); right.append(a)
        return (left, right) if left else (None, None)

    def left_key(self, row):
        return tuple(evaluate(e, row) for e in self.left_keys)

    def right_key(self, row):
        pad = [None] * self.offset
        return tuple(evaluate(e, pad + row) for e in self.right_keys)

    def bind(self, index, key, entry):
        if None in key: return
        index.setdefault(key, set()).add(entry)

    def unbind(self, index, key, entry):
        bucket = index.get(key)
        if bucket is None: return
        bucket.discard(entry)
        if not bucket: del index[key]

    def probe_right(self, lrow):
        """Right rows that could satisfy ON for this left row."""
        if self.left_keys is None: return list(self.right.items())
        key = self.left_key(lrow)
        if None in key: return ()
        return [(k, self.right[k]) for k in self.rindex.get(key, ())]

    def probe_left(self, rrow):
        if self.left_keys is None: return list(self.left.items())
        key = self.right_key(rrow)
        if None in key: return ()
        return [(k, self.left[k]) for k in self.lindex.get(key, ())]

    def add(self, key, row, side):
        if side == 'left': self.add_left(key, row)
        else: self.add_right(key, row)

    def remove(self, key, row, side):
        if side == 'left': self.remove_left(key, row)
        else: self.remove_right(key, row)

    def add_left(self, lkey, lrow):
        if self.left_keys is not None: self.bind(self.lindex, self.left_key(lrow), lkey)
        matched = 0
        for rkey, rrow in self.probe_right(lrow):
            if true(evaluate(self.on, lrow + rrow)):
                self.emit(lkey + rkey, lrow + rrow)
                matched += 1
        if self.outer:
            self.matches[lkey] = matched
            if not matched: self.emit(lkey + (PAD,), lrow + [None] * self.width)

    def remove_left(self, lkey, lrow):
        if self.left_keys is not None: self.unbind(self.lindex, self.left_key(lrow), lkey)
        for out in list(self.by_left.get(lkey, ())): self.retract(out)
        self.by_left.pop(lkey, None)
        self.matches.pop(lkey, None)

    def add_right(self, rkey, rrow):
        if self.left_keys is not None: self.bind(self.rindex, self.right_key(rrow), rkey)
        for lkey, lrow in self.probe_left(rrow):
            if not true(evaluate(self.on, lrow + rrow)): continue
            if self.outer:
                if self.matches[lkey] == 0: self.retract(lkey + (PAD,))
                self.matches[lkey] += 1
            self.emit(lkey + rkey, lrow + rrow)

    def remove_right(self, rkey, rrow):
        if self.left_keys is not None: self.unbind(self.rindex, self.right_key(rrow), rkey)
        dropped = list(self.by_right.pop(rkey, ()))
        for out in dropped: self.retract(out)
        if not self.outer: return
        for out in dropped:
            lkey = out[:-1]
            if lkey not in self.matches: continue
            self.matches[lkey] -= 1
            if self.matches[lkey] == 0:
                self.emit(lkey + (PAD,), self.left[lkey] + [None] * self.width)

    def emit(self, key, row):
        self.rows[key] = row
        self.by_left.setdefault(key[:-1], set()).add(key)
        if key[-1] != PAD: self.by_right.setdefault((key[-1],), set()).add(key)
        self.down.add(key, row, self.side)

    def retract(self, key):
        row = self.rows.pop(key, None)
        if row is None: return
        bucket = self.by_left.get(key[:-1])
        if bucket is not None: bucket.discard(key)
        if key[-1] != PAD:
            bucket = self.by_right.get((key[-1],))
            if bucket is not None: bucket.discard(key)
        self.down.remove(key, row, self.side)


class Accumulator:
    """Running value of one aggregate over the live members of a group."""

    def __init__(self, node):
        self.op = node.op
        self.arg = node.args[0] if node.args else None
        self.count = 0
        self.total = 0
        self.values = {}
        self.extreme = None
        self.known = False

    def add(self, row):
        if self.arg is None:
            self.count += 1
            return
        v = evaluate(self.arg, row)
        if v is None: return
        self.count += 1
        if self.op == 'SUM':
            self.total += v
        elif self.op in ('MIN', 'MAX'):
            self.values[v] = self.values.get(v, 0) + 1
            # While the extreme is unknown a single value proves nothing; it is
            # recovered from the multiset on the next read of this group.
            if self.known and (v < self.extreme if self.op == 'MIN' else v > self.extreme):
                self.extreme = v

    def remove(self, row):
        if self.arg is None:
            self.count -= 1
            return
        v = evaluate(self.arg, row)
        if v is None: return
        self.count -= 1
        if self.op == 'SUM':
            self.total -= v
        elif self.op in ('MIN', 'MAX'):
            rest = self.values[v] - 1
            if rest:
                self.values[v] = rest
            else:
                del self.values[v]
                # Losing the extreme is the one case that revisits the group.
                if self.known and v == self.extreme: self.known = False

    def value(self):
        if self.op == 'COUNT': return self.count
        if not self.count: return None
        if self.op == 'SUM': return self.total
        if not self.known:
            self.extreme = (min if self.op == 'MIN' else max)(self.values)
            self.known = True
        return self.extreme


class Group:
    __slots__ = ('members', 'accumulators', 'first', 'projected', 'passes')

    def __init__(self, accumulators):
        self.members = {}
        self.accumulators = accumulators
        self.first = None
        self.projected = ()
        self.passes = False


class Sink:
    """WHERE, then either grouped aggregation or direct projection."""

    def __init__(self, plan):
        q = plan.query
        self.q = q
        self.where = q.where
        self.select = q.select
        self.having = q.having
        self.aggregate = plan.aggregate
        self.out = {}
        self.groups = {}
        self.assigned = {}
        self.dirty = set()
        roots = [e for e, _ in q.select] + ([q.having] if q.having else [])
        nodes = {}
        for root in roots:
            for n in walk(root):
                if n.op in AGGREGATES: nodes.setdefault(id(n), n)
        self.aggregates = list(nodes.values())
        if self.aggregate and not q.groups: self.group(())

    def group(self, gkey):
        g = self.groups.get(gkey)
        if g is None:
            g = self.groups[gkey] = Group([Accumulator(n) for n in self.aggregates])
            self.dirty.add(gkey)
        return g

    def add(self, key, row, side=None):
        if self.where is not None and not true(evaluate(self.where, row)): return
        if not self.aggregate:
            self.out[key] = tuple(evaluate(e, row) for e, _ in self.select)
            return
        gkey = tuple(evaluate(e, row) for e in self.q.groups)
        g = self.group(gkey)
        g.members[key] = row
        # `first` is the group's encounter position; None means it must be
        # recovered from the members, so a new row may not simply claim it.
        if len(g.members) == 1: g.first = key
        elif g.first is not None and key < g.first: g.first = key
        for a in g.accumulators: a.add(row)
        self.assigned[key] = gkey
        self.dirty.add(gkey)

    def remove(self, key, row, side=None):
        if not self.aggregate:
            self.out.pop(key, None)
            return
        gkey = self.assigned.pop(key, None)
        if gkey is None: return
        g = self.groups[gkey]
        del g.members[key]
        for a in g.accumulators: a.remove(row)
        if key == g.first: g.first = None
        if not g.members and self.q.groups:
            del self.groups[gkey]
            self.dirty.discard(gkey)
        else:
            self.dirty.add(gkey)

    def refresh(self, gkey):
        g = self.groups[gkey]
        slots = Slots()
        for node, a in zip(self.aggregates, g.accumulators): slots[id(node)] = a.value()
        # Every non-aggregate reference is a group key, so any member represents
        # the group; an empty global group has no references to evaluate.
        rep = next(iter(g.members.values())) if g.members else []
        g.passes = self.having is None or true(evaluate(self.having, rep, slots))
        g.projected = tuple(evaluate(e, rep, slots) for e, _ in self.select)

    def result(self):
        if not self.aggregate:
            return [list(self.out[k]) for k in sorted(self.out)]
        for gkey in self.dirty:
            if gkey in self.groups: self.refresh(gkey)
        self.dirty.clear()
        groups = list(self.groups.values())
        if self.q.groups:
            for g in groups:
                if g.first is None: g.first = min(g.members)
            groups.sort(key=lambda g: g.first)
        return [list(g.projected) for g in groups if g.passes]


class View:
    """A named query kept up to date by pushing base-table deltas through it."""

    def __init__(self, plan):
        q = plan.query
        self.plan = plan
        self.tables = [name for name, _ in q.sources]
        widths = [len(t['columns']) for t in plan.tables]
        offsets, total = [], 0
        for w in widths:
            offsets.append(total)
            total += w
        self.sources = [Source(offsets[i], plan.filters[i]) for i in range(len(widths))]
        upstream = self.sources[0]
        for j, join in enumerate(q.joins):
            node = JoinNode(j + 1, upstream.rows, self.sources[j + 1].rows, join.on,
                            join.outer, offsets[j + 1], widths[j + 1])
            upstream.down, upstream.side = node, 'left'
            self.sources[j + 1].down, self.sources[j + 1].side = node, 'right'
            upstream = node
        self.sink = Sink(plan)
        upstream.down, upstream.side = self.sink, None

    def load(self, tables):
        """Initial population; right inputs first so left rows join immediately."""
        for i in reversed(range(len(self.sources))):
            for seq, row in tables[self.tables[i]].live():
                self.sources[i].add(seq, row)

    def apply(self, name, removed, added):
        for i, table in enumerate(self.tables):
            if table != name: continue
            for seq, row in removed: self.sources[i].remove(seq, row)
            for seq, row in added: self.sources[i].add(seq, row)

    def read(self):
        return finish(self.plan.query, self.sink.result())
