"""Incremental view maintenance built on the shared parser/binder/engine."""
from model import Expr, require
from binder import walk
from engine import evaluate, fold, sort_rows
from parser import AGGREGATES


def pad(row, offset):
    return [None] * offset + list(row)


def extract_equality(on_expr, left_width):
    if on_expr.op != '=':
        return None
    a, b = on_expr.args
    if a.op != 'col' or b.op != 'col':
        return None
    if a.index < left_width <= b.index:
        return (a.index, b.index - left_width)
    if b.index < left_width <= a.index:
        return (b.index, a.index - left_width)
    return None


def scan_stage(diffs, filters, offset):
    """diffs: list of (id, old_or_None, new_or_None). Pure function of the filter predicates."""
    events = []
    for id_, old, new in diffs:
        old_pass = old is not None and all(evaluate(p, pad(old, offset)) for p in filters)
        new_pass = new is not None and all(evaluate(p, pad(new, offset)) for p in filters)
        old_out = old if old_pass else None
        new_out = new if new_pass else None
        if old_out is None and new_out is None:
            continue
        events.append((id_, old_out, new_out))
    return events


class JoinLevel:
    """Maintains join level i (1-based join step) incrementally."""

    def __init__(self, on_expr, kind, prev_width, right_width):
        self.on = on_expr
        self.kind = kind
        self.prev_width = prev_width
        self.right_width = right_width
        self.groups = {}       # pk -> {ck_or_None: full_row}
        self.pk_row = {}       # pk -> prev-level row (concatenated, width=prev_width)
        self.owner = {}        # id -> set(pk) currently matched to id
        self.scan = {}         # id -> row (this level's own filtered scan)
        self.eq = extract_equality(on_expr, prev_width)
        self.key_of_pk = {}
        self.pk_by_key = {}
        self.key_of_id = {}
        self.id_by_key = {}

    def _pk_key(self, prevrow):
        if self.eq is None:
            return None
        return prevrow[self.eq[0]]

    def _id_key(self, row):
        if self.eq is None:
            return None
        return row[self.eq[1]]

    def _index_pk(self, pk, prevrow):
        self.pk_row[pk] = prevrow
        if self.eq is not None:
            k = self._pk_key(prevrow)
            self.key_of_pk[pk] = k
            if k is not None:
                self.pk_by_key.setdefault(k, set()).add(pk)

    def _deindex_pk(self, pk):
        k = self.key_of_pk.pop(pk, None)
        if k is not None:
            s = self.pk_by_key.get(k)
            if s is not None:
                s.discard(pk)
                if not s:
                    del self.pk_by_key[k]
        self.pk_row.pop(pk, None)

    def _index_id(self, id_, row):
        self.scan[id_] = row
        if self.eq is not None:
            k = self._id_key(row)
            self.key_of_id[id_] = k
            if k is not None:
                self.id_by_key.setdefault(k, set()).add(id_)

    def _deindex_id(self, id_):
        k = self.key_of_id.pop(id_, None)
        if k is not None:
            s = self.id_by_key.get(k)
            if s is not None:
                s.discard(id_)
                if not s:
                    del self.id_by_key[k]
        self.scan.pop(id_, None)

    def _pk_candidates(self, row):
        if self.eq is not None:
            k = self._id_key(row)
            if k is None:
                return set()
            return set(self.pk_by_key.get(k, ()))
        return set(self.groups.keys())

    def _id_candidates(self, prevrow):
        if self.eq is not None:
            k = self._pk_key(prevrow)
            if k is None:
                return set()
            return set(self.id_by_key.get(k, ()))
        return set(self.scan.keys())

    def _remove_match(self, pk, id_, events, add=True):
        sub = self.groups.get(pk)
        if sub is None or id_ not in sub:
            return
        row = sub.pop(id_)
        events.setdefault('removed', []).append((pk + (id_,), row))
        if id_ is not None:
            s = self.owner.get(id_)
            if s is not None:
                s.discard(pk)
        if add and not sub and self.kind == 'LEFT':
            placeholder = list(self.pk_row[pk]) + [None] * self.right_width
            sub[None] = placeholder
            events.setdefault('added', []).append((pk + (None,), placeholder))

    def _add_match(self, pk, id_, row, events):
        sub = self.groups.setdefault(pk, {})
        if None in sub:
            ph = sub.pop(None)
            events.setdefault('removed', []).append((pk + (None,), ph))
        sub[id_] = row
        events.setdefault('added', []).append((pk + (id_,), row))
        if id_ is not None:
            self.owner.setdefault(id_, set()).add(pk)

    def process(self, delta_in, own_events):
        """delta_in: list of (pk, old_prevrow_or_None, new_prevrow_or_None).
        own_events: list of (id, old_row_or_None, new_row_or_None) at this source.
        Returns list of (key, old_or_None, new_or_None) for level output."""
        removed = {}
        added = {}

        def emit_removed(key, row):
            removed[key] = row

        def emit_added(key, row):
            added[key] = row

        events = {}

        # Step 1: own scan delta vs OLD prev state.
        for id_, old, new in own_events:
            if old is not None:
                candidates = set(self.owner.get(id_, ()))
                for pk in candidates:
                    prevrow = self.pk_row.get(pk)
                    if prevrow is not None and evaluate(self.on, list(prevrow) + list(old)):
                        self._remove_match(pk, id_, events)
            if new is not None:
                candidates = self._pk_candidates(new)
                sub_has = set()
                for pk in candidates:
                    sub = self.groups.get(pk)
                    if sub is not None and id_ in sub:
                        sub_has.add(pk)
                for pk in candidates - sub_has:
                    prevrow = self.pk_row.get(pk)
                    if prevrow is not None and evaluate(self.on, list(prevrow) + list(new)):
                        self._add_match(pk, id_, list(prevrow) + list(new), events)
            if old is not None:
                self._deindex_id(id_)
            if new is not None:
                self._index_id(id_, new)

        # Step 2: delta_in vs NEW scan state.
        for pk, old_prev, new_prev in delta_in:
            if old_prev is not None:
                sub = self.groups.pop(pk, None)
                if sub is not None:
                    for ck, row in list(sub.items()):
                        events.setdefault('removed', []).append((pk + (ck,), row))
                        if ck is not None:
                            s = self.owner.get(ck)
                            if s is not None:
                                s.discard(pk)
                self._deindex_pk(pk)
            if new_prev is not None:
                self._index_pk(pk, new_prev)
                sub = {}
                candidates = self._id_candidates(new_prev)
                for id_ in candidates:
                    row = self.scan.get(id_)
                    if row is not None and evaluate(self.on, list(new_prev) + list(row)):
                        sub[id_] = list(new_prev) + list(row)
                        events.setdefault('added', []).append((pk + (id_,), sub[id_]))
                        self.owner.setdefault(id_, set()).add(pk)
                if not sub and self.kind == 'LEFT':
                    placeholder = list(new_prev) + [None] * self.right_width
                    sub[None] = placeholder
                    events.setdefault('added', []).append((pk + (None,), placeholder))
                if sub:
                    self.groups[pk] = sub

        removed_list = events.get('removed', [])
        added_list = events.get('added', [])
        removed_map = {}
        for k, r in removed_list:
            removed_map[k] = r
        added_map = {}
        for k, r in added_list:
            added_map[k] = r
        order = []
        seen = set()
        for k, _ in removed_list + added_list:
            if k not in seen:
                seen.add(k)
                order.append(k)
        return [(k, removed_map.get(k), added_map.get(k)) for k in order]


def substitute_aggs(e, values):
    if e.op in AGGREGATES:
        return Expr('lit', values[id(e)], type=e.type)
    return Expr(e.op, e.value, [substitute_aggs(a, values) for a in e.args], e.type, e.index, e.source)


class View:
    def __init__(self, plan):
        self.plan = plan
        q = plan.query
        self.n = len(plan.tables)
        self.source_names = [name for name, alias in q.sources]
        self.widths = [len(t['columns']) for t in plan.tables]
        self.offsets = []
        acc = 0
        for w in self.widths:
            self.offsets.append(acc)
            acc += w
        self.filters = plan.filters
        self.levels = [None] + [
            JoinLevel(q.joins[i - 1], q.join_kinds[i - 1], self.offsets[i], self.widths[i])
            for i in range(1, self.n)
        ]
        self.finalrows = {}
        self.groups = {}
        self.contrib = {}
        roots = [e for e, _ in q.select] + ([q.having] if q.having else [])
        seen = set()
        self.agg_nodes = []
        for root in roots:
            for node in walk(root):
                if node.op in AGGREGATES and id(node) not in seen:
                    seen.add(id(node))
                    self.agg_nodes.append(node)
        if plan.aggregate and not q.groups:
            self.groups[()] = {'n': 0, 'rep': [], 'nodes': {}}
        initial = {i: [(id_, None, row) for id_, row in plan.tables[i]['rows'].items()] for i in range(self.n)}
        self.process(initial)

    def process(self, diffs_by_index):
        events = []
        for i in range(self.n):
            diffs_i = diffs_by_index.get(i, [])
            own = scan_stage(diffs_i, self.filters[i], self.offsets[i]) if diffs_i else []
            if i == 0:
                events = [((id_,), old, new) for id_, old, new in own]
            else:
                if not events and not own:
                    events = []
                else:
                    events = self.levels[i].process(events, own)
        where_events = self.where_stage(events)
        if self.plan.aggregate:
            self.update_groups(where_events)
        else:
            self.update_finalrows(where_events)

    def where_stage(self, events):
        where = self.plan.query.where
        out = []
        for k, old, new in events:
            old_pass = old is not None and (where is None or evaluate(where, list(old)) is True)
            new_pass = new is not None and (where is None or evaluate(where, list(new)) is True)
            o = old if old_pass else None
            n = new if new_pass else None
            if o is None and n is None:
                continue
            out.append((k, o, n))
        return out

    def update_finalrows(self, events):
        for k, old, new in events:
            if new is None:
                self.finalrows.pop(k, None)
            else:
                self.finalrows[k] = new

    def update_groups(self, events):
        q = self.plan.query
        for k, old, new in events:
            if old is not None:
                gk = tuple(evaluate(g, list(old)) for g in q.groups)
                self._remove_row(gk, old)
                self.contrib.pop(k, None)
            if new is not None:
                gk = tuple(evaluate(g, list(new)) for g in q.groups)
                self._add_row(gk, new)
                self.contrib[k] = gk

    def _add_row(self, gk, row):
        acc = self.groups.get(gk)
        if acc is None:
            acc = {'n': 0, 'rep': row, 'nodes': {}}
            self.groups[gk] = acc
        acc['n'] += 1
        if acc['n'] == 1:
            acc['rep'] = row
        for node in self.agg_nodes:
            st = acc['nodes'].setdefault(id(node), {'nonnull': 0, 'total': 0, 'values': {}})
            if node.op == 'COUNT':
                if node.args:
                    v = evaluate(node.args[0], row)
                    if v is not None:
                        st['nonnull'] += 1
            elif node.op == 'SUM':
                v = evaluate(node.args[0], row)
                if v is not None:
                    st['total'] += v
                    st['nonnull'] += 1
            elif node.op in ('MIN', 'MAX'):
                v = evaluate(node.args[0], row)
                if v is not None:
                    st['values'][v] = st['values'].get(v, 0) + 1

    def _remove_row(self, gk, row):
        acc = self.groups.get(gk)
        if acc is None:
            return
        acc['n'] -= 1
        for node in self.agg_nodes:
            st = acc['nodes'].get(id(node))
            if st is None:
                continue
            if node.op == 'COUNT':
                if node.args:
                    v = evaluate(node.args[0], row)
                    if v is not None:
                        st['nonnull'] -= 1
            elif node.op == 'SUM':
                v = evaluate(node.args[0], row)
                if v is not None:
                    st['total'] -= v
                    st['nonnull'] -= 1
            elif node.op in ('MIN', 'MAX'):
                v = evaluate(node.args[0], row)
                if v is not None:
                    st['values'][v] -= 1
                    if st['values'][v] == 0:
                        del st['values'][v]
        if acc['n'] == 0 and self.plan.query.groups:
            del self.groups[gk]

    def _agg_value(self, node, acc):
        st = acc['nodes'].get(id(node))
        if node.op == 'COUNT':
            return acc['n'] if not node.args else (st['nonnull'] if st else 0)
        if node.op == 'SUM':
            return st['total'] if st and st['nonnull'] > 0 else None
        if st is None or not st['values']:
            return None
        return min(st['values']) if node.op == 'MIN' else max(st['values'])

    def read_result(self):
        q = self.plan.query
        projected = []
        if self.plan.aggregate:
            for gk, acc in self.groups.items():
                rep = list(acc['rep'])
                values = {id(node): self._agg_value(node, acc) for node in self.agg_nodes}
                if q.having is not None:
                    hv = evaluate(substitute_aggs(q.having, values), rep)
                    if hv is not True:
                        continue
                projected.append([evaluate(substitute_aggs(e, values), rep) for e, _ in q.select])
        else:
            for row in self.finalrows.values():
                projected.append([evaluate(e, list(row)) for e, _ in q.select])
        if q.distinct:
            projected = [list(r) for r in dict.fromkeys(tuple(r) for r in projected)]
        if q.order:
            projected = sort_rows(projected, q.order)
        projected = projected[q.offset: None if q.limit is None else q.offset + q.limit]
        return {'columns': [a for _, a in q.select], 'rows': projected}
