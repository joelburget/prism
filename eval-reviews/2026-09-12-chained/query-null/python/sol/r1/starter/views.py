"""Incrementally maintained materialized views over the bound query plans."""
from itertools import product

from engine import evaluate, format_projected


class MaterializedView:
    def __init__(self, plan, database):
        self.plan = plan
        self.joined = {}
        self.projected = {}
        self.groups = {}
        self.group_values = {}
        self._attach(database)
        affected = self._add_candidates(database, None)
        if plan.aggregate and not plan.query.groups:
            self.groups.setdefault((), {})
            affected.add(())
        self._refresh_groups(affected)

    def _attach(self, database):
        self.plan.tables = [database[name] for name, _ in self.plan.query.sources]

    def _scans(self, database):
        scans, offset = [], 0
        for table, predicates in zip(self.plan.tables, self.plan.filters):
            rows = []
            for row_id, row in table['_rows_by_id'].items():
                if all(evaluate(p, [None] * offset + row) is True for p in predicates):
                    rows.append((row_id, row))
            scans.append(rows)
            offset += len(table['columns'])
        return scans

    def _valid_combination(self, choices, scans):
        key = []
        first_id, first_row = choices[0]
        if first_id is None: return None
        key.append(first_id)
        flat = list(first_row)
        for i, choice in enumerate(choices[1:], 1):
            row_id, row = choice
            kind, on = self.plan.query.joins[i - 1]
            if row_id is None:
                if kind != 'left': return None
                if any(evaluate(on, flat + candidate) is True for _, candidate in scans[i]):
                    return None
                flat += [None] * len(self.plan.tables[i]['columns'])
            else:
                if evaluate(on, flat + row) is not True: return None
                flat += row
            key.append(row_id)
        if self.plan.query.where and evaluate(self.plan.query.where, flat) is not True:
            return None
        return tuple(key), flat

    def _add_candidates(self, database, touched):
        scans = self._scans(database)
        choices = [scan if i == 0 else scan + [(None, None)]
                   for i, scan in enumerate(scans)]
        if not choices or any(not c for c in choices): return set()
        affected_groups = set()
        for combination in product(*choices):
            if touched is not None and not any(
                    rid in touched.get(name, set()) or
                    (rid is None and name in touched)
                    for (name, _), (rid, _) in zip(self.plan.query.sources, combination)):
                continue
            valid = self._valid_combination(combination, scans)
            if valid is None: continue
            key, row = valid
            if key in self.joined: continue
            self.joined[key] = row
            affected_groups |= self._add_contribution(key, row)
        return affected_groups

    def _add_contribution(self, key, row):
        q = self.plan.query
        if not self.plan.aggregate:
            self.projected[key] = [evaluate(e, row) for e, _ in q.select]
            return set()
        group_key = tuple(evaluate(e, row) for e in q.groups)
        self.groups.setdefault(group_key, {})[key] = row
        return {group_key}

    def _remove_contribution(self, key, row):
        if not self.plan.aggregate:
            self.projected.pop(key, None)
            return set()
        group_key = tuple(evaluate(e, row) for e in self.plan.query.groups)
        members = self.groups[group_key]
        members.pop(key)
        if not members and self.plan.query.groups:
            self.groups.pop(group_key)
            self.group_values.pop(group_key, None)
        return {group_key}

    def _refresh_groups(self, keys):
        q = self.plan.query
        for group_key in keys:
            members = self.groups.get(group_key)
            if members is None: continue
            group = list(members.values())
            row = group[0] if group else []
            if q.having is not None and evaluate(q.having, row, group) is not True:
                self.group_values.pop(group_key, None)
            else:
                self.group_values[group_key] = [evaluate(e, row, group) for e, _ in q.select]

    def apply(self, database, touched):
        """Retract stale ID combinations, then derive combinations affected by the batch."""
        self._attach(database)
        affected = set()
        source_names = [name for name, _ in self.plan.query.sources]
        for key in list(self.joined):
            if any((rid in touched.get(name, set())) or (rid is None and name in touched)
                   for name, rid in zip(source_names, key)):
                row = self.joined.pop(key)
                affected |= self._remove_contribution(key, row)
        affected |= self._add_candidates(database, touched)
        if self.plan.aggregate and not self.plan.query.groups:
            self.groups.setdefault((), {})
            affected.add(())
        self._refresh_groups(affected)

    def _row_positions(self):
        positions = [{row_id: i for i, row_id in enumerate(t['_rows_by_id'])}
                     for t in self.plan.tables]
        return lambda key: tuple(-1 if rid is None else positions[i].get(rid, -1)
                                 for i, rid in enumerate(key))

    def read(self):
        order_key = self._row_positions()
        if not self.plan.aggregate:
            values = [self.projected[k] for k in sorted(self.projected, key=order_key)]
        else:
            def group_order(item):
                members = self.groups[item]
                return min((order_key(k) for k in members), default=())
            values = [self.group_values[k] for k in sorted(self.group_values, key=group_order)]
        return format_projected(self.plan, values)
