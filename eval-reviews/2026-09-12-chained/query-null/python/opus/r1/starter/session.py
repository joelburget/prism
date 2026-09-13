"""Command protocol: mutable base tables, batch atomicity, and named views."""
import re
from model import DomainError, require
from parser import Parser
from binder import bind
from engine import optimize
from views import View

VIEW_NAME = re.compile('[a-z][a-z0-9-]{0,39}')
TABLE_NAME = re.compile('[a-z_][a-z0-9_]*')
MAX_ID = 2147483647
BOUND = 1000000000
KINDS = {'int': int, 'text': str, 'bool': bool}


def shape(value, expected, code='INVALID_COMMAND'):
    require(isinstance(value, dict) and set(value) == expected, code)


class Table:
    """Base rows addressed by caller-chosen identifiers.

    `seq` is a private, monotonically increasing encounter number: initial rows
    take 1..N in input order, an insert appends the next one, and an update
    keeps the row's own, which is exactly the position rule of the contract.
    """

    def __init__(self, spec):
        self.columns = spec['columns']
        self.rows = {}
        self.used = set()
        self.next_seq = 1
        for row in spec['rows']:
            self.rows[self.next_seq] = (self.next_seq, list(row))
            self.used.add(self.next_seq)
            self.next_seq += 1

    def live(self):
        return sorted(self.rows.values())

    def coerce(self, row):
        require(len(row) == len(self.columns), 'INVALID_ROW')
        for v, c in zip(row, self.columns):
            if v is None:
                require(c['nullable'], 'INVALID_ROW')
            else:
                require(type(v) is KINDS[c['type']], 'INVALID_ROW')
                require(c['type'] != 'int' or -BOUND <= v <= BOUND, 'INVALID_ROW')
        return list(row)


class Session:
    def __init__(self, database):
        self.database = database
        self.tables = {name: Table(spec) for name, spec in database.items()}
        self.views = {}
        self.revision = 0

    def run(self, commands):
        replies = []
        for command in commands:
            try:
                replies.append({'ok': True, 'result': self.dispatch(command)})
            except DomainError as e:
                replies.append({'ok': False, 'error': {'code': str(e)}})
        return replies

    def dispatch(self, command):
        require(isinstance(command, dict) and isinstance(command.get('op'), str), 'INVALID_COMMAND')
        handler = {'create': self.create, 'read': self.read, 'drop': self.drop, 'apply': self.batch}.get(command['op'])
        require(handler is not None, 'INVALID_COMMAND')
        return handler(command)

    def named(self, command, expected):
        shape(command, expected)
        name = command['view']
        require(isinstance(name, str) and VIEW_NAME.fullmatch(name), 'INVALID_COMMAND')
        return name

    def create(self, command):
        name = self.named(command, {'op', 'view', 'sql', 'optimize'})
        require(isinstance(command['sql'], str) and type(command['optimize']) is bool, 'INVALID_COMMAND')
        require(name not in self.views, 'VIEW_EXISTS')
        plan = bind(Parser(command['sql']).parse(), self.database)
        if command['optimize']: plan = optimize(plan)
        view = View(plan)
        view.load(self.tables)
        self.views[name] = view
        return {'view': name, 'revision': self.revision}

    def read(self, command):
        name = self.named(command, {'op', 'view'})
        require(name in self.views, 'UNKNOWN_VIEW')
        result = self.views[name].read()
        return {'revision': self.revision, 'columns': list(result['columns']), 'rows': result['rows']}

    def drop(self, command):
        name = self.named(command, {'op', 'view'})
        require(name in self.views, 'UNKNOWN_VIEW')
        del self.views[name]
        return {'dropped': name}

    def batch(self, command):
        shape(command, {'op', 'changes'})
        changes = command['changes']
        require(isinstance(changes, list) and 1 <= len(changes) <= 200, 'INVALID_COMMAND')
        for change in changes: check(change)
        before, added, seqs = {}, [], {}
        try:
            for change in changes: self.stage(change, before, added, seqs)
        except DomainError:
            self.rollback(before, added, seqs)
            raise
        self.commit(before)
        self.revision += 1
        return {'revision': self.revision}

    def stage(self, change, before, added, seqs):
        """Apply one change to the shared tables, recording enough to undo it."""
        op, name, rid = change['op'], change['table'], change['id']
        require(name in self.tables, 'UNKNOWN_TABLE')
        table = self.tables[name]
        row = table.coerce(change['row']) if op != 'delete' else None
        if op == 'insert':
            require(rid not in table.used, 'ROW_ID_USED')
        else:
            require(rid in table.rows, 'UNKNOWN_ROW')
        before.setdefault((name, rid), table.rows.get(rid))
        seqs.setdefault(name, table.next_seq)
        if op == 'insert':
            table.used.add(rid)
            added.append((name, rid))
            table.rows[rid] = (table.next_seq, row)
            table.next_seq += 1
        elif op == 'update':
            table.rows[rid] = (table.rows[rid][0], row)
        else:
            del table.rows[rid]

    def rollback(self, before, added, seqs):
        for (name, rid), entry in before.items():
            table = self.tables[name]
            if entry is None: table.rows.pop(rid, None)
            else: table.rows[rid] = entry
        for name, rid in added: self.tables[name].used.discard(rid)
        for name, seq in seqs.items(): self.tables[name].next_seq = seq

    def commit(self, before):
        """Turn the net per-row effect of the batch into view deltas."""
        deltas = {}
        for (name, rid), old in before.items():
            new = self.tables[name].rows.get(rid)
            if old == new: continue
            removed, inserted = deltas.setdefault(name, ([], []))
            if old is not None: removed.append(old)
            if new is not None: inserted.append(new)
        for view in self.views.values():
            for name, (removed, inserted) in deltas.items():
                view.apply(name, removed, inserted)


def check(change):
    """Shape and scalar constraints, validated for every change up front."""
    require(isinstance(change, dict) and change.get('op') in ('insert', 'update', 'delete'), 'INVALID_COMMAND')
    op = change['op']
    shape(change, {'op', 'table', 'id'} | ({'row'} if op != 'delete' else set()))
    require(isinstance(change['table'], str) and TABLE_NAME.fullmatch(change['table']), 'INVALID_COMMAND')
    require(type(change['id']) is int and 1 <= change['id'] <= MAX_ID, 'INVALID_COMMAND')
    require(op == 'delete' or isinstance(change['row'], list), 'INVALID_COMMAND')
