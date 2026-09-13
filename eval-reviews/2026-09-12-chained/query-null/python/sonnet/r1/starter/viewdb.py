"""Command-mode driver: base tables with row IDs, views, atomic batched changes."""
import re
from model import require, DomainError
from parser import Parser
from binder import bind
from engine import optimize
from incview import View

VIEW_RE = re.compile(r'^[a-z][a-z0-9-]{0,39}$')
TABLE_RE = re.compile(r'^[a-z_][a-z0-9_]*$')
MAX_COMMANDS = 2000
MAX_CHANGES = 200
BOUND = 1_000_000_000


class Table:
    def __init__(self, tdef):
        self.name = tdef['name']
        self.columns = tdef['columns']
        self.rows = {}
        self.used_ids = set()
        for i, row in enumerate(tdef['rows'], start=1):
            self.rows[i] = tuple(row)
            self.used_ids.add(i)

    def as_binder_table(self):
        return {'name': self.name, 'columns': self.columns, 'rows': self.rows}


def valid_row(row, columns):
    if not isinstance(row, list) or len(row) != len(columns):
        return False
    for v, c in zip(row, columns):
        if v is None:
            if not c['nullable']:
                return False
        elif c['type'] == 'int':
            if type(v) is not int or isinstance(v, bool) or not (-BOUND <= v <= BOUND):
                return False
        elif c['type'] == 'bool':
            if type(v) is not bool:
                return False
        else:
            if type(v) is not str:
                return False
    return True


def command_error(code):
    return {'ok': False, 'error': {'code': code}}


def command_ok(value):
    return {'ok': True, 'result': value}


def shape_check(cmd):
    require(isinstance(cmd, dict) and 'op' in cmd, 'INVALID_COMMAND')
    op = cmd.get('op')
    if op == 'create':
        require(set(cmd) == {'op', 'view', 'sql', 'optimize'}, 'INVALID_COMMAND')
        require(isinstance(cmd['view'], str) and bool(VIEW_RE.match(cmd['view'])), 'INVALID_COMMAND')
        require(isinstance(cmd['sql'], str), 'INVALID_COMMAND')
        require(type(cmd['optimize']) is bool, 'INVALID_COMMAND')
    elif op in ('read', 'drop'):
        require(set(cmd) == {'op', 'view'}, 'INVALID_COMMAND')
        require(isinstance(cmd['view'], str) and bool(VIEW_RE.match(cmd['view'])), 'INVALID_COMMAND')
    elif op == 'apply':
        require(set(cmd) == {'op', 'changes'}, 'INVALID_COMMAND')
        changes = cmd['changes']
        require(isinstance(changes, list) and 1 <= len(changes) <= MAX_CHANGES, 'INVALID_COMMAND')
        for ch in changes:
            require(isinstance(ch, dict) and 'op' in ch, 'INVALID_COMMAND')
            cop = ch.get('op')
            if cop in ('insert', 'update'):
                require(set(ch) == {'op', 'table', 'id', 'row'}, 'INVALID_COMMAND')
            elif cop == 'delete':
                require(set(ch) == {'op', 'table', 'id'}, 'INVALID_COMMAND')
            else:
                raise DomainError('INVALID_COMMAND')
            require(isinstance(ch['table'], str) and bool(TABLE_RE.match(ch['table'])), 'INVALID_COMMAND')
            require(type(ch['id']) is int and 1 <= ch['id'] <= 2147483647, 'INVALID_COMMAND')
            if cop in ('insert', 'update'):
                require(isinstance(ch['row'], list), 'INVALID_COMMAND')
    else:
        raise DomainError('INVALID_COMMAND')
    return op


def do_create(views, tables, cmd, revision):
    name = cmd['view']
    if name in views:
        return command_error('VIEW_EXISTS')
    try:
        binder_db = {n: t.as_binder_table() for n, t in tables.items()}
        plan = bind(Parser(cmd['sql']).parse(), binder_db)
        if cmd['optimize']:
            plan = optimize(plan)
        view = View(plan)
    except DomainError as e:
        return command_error(str(e))
    views[name] = view
    return command_ok({'view': name, 'revision': revision})


def do_read(views, cmd, revision):
    name = cmd['view']
    if name not in views:
        return command_error('UNKNOWN_VIEW')
    result = views[name].read_result()
    return command_ok({'revision': revision, 'columns': result['columns'], 'rows': result['rows']})


def do_drop(views, cmd):
    name = cmd['view']
    if name not in views:
        return command_error('UNKNOWN_VIEW')
    del views[name]
    return command_ok({'dropped': name})


def do_apply(tables, views, cmd, revision):
    changes = cmd['changes']
    touched = {}
    for ch in changes:
        name = ch['table']
        if name not in tables:
            return command_error('UNKNOWN_TABLE'), revision
        if name not in touched:
            t = tables[name]
            touched[name] = [dict(t.rows), set(t.used_ids)]
        rows, used = touched[name]
        columns = tables[name].columns
        cop = ch['op']
        if cop == 'insert':
            if not valid_row(ch['row'], columns):
                return command_error('INVALID_ROW'), revision
            if ch['id'] in used:
                return command_error('ROW_ID_USED'), revision
            rows[ch['id']] = tuple(ch['row'])
            used.add(ch['id'])
        elif cop == 'update':
            if not valid_row(ch['row'], columns):
                return command_error('INVALID_ROW'), revision
            if ch['id'] not in rows:
                return command_error('UNKNOWN_ROW'), revision
            rows[ch['id']] = tuple(ch['row'])
        else:
            if ch['id'] not in rows:
                return command_error('UNKNOWN_ROW'), revision
            del rows[ch['id']]
    for name, (rows, used) in touched.items():
        t = tables[name]
        old_rows = t.rows
        changed_ids = set(old_rows) | set(rows)
        diffs = [(i, old_rows.get(i), rows.get(i)) for i in changed_ids if old_rows.get(i) != rows.get(i)]
        t.rows = rows
        t.used_ids = used
        if diffs:
            for view in views.values():
                idxs = [i for i, n in enumerate(view.source_names) if n == name]
                if idxs:
                    view.process({i: diffs for i in idxs})
    return command_ok({'revision': revision + 1}), revision + 1


def run_commands(database, commands):
    require(len(commands) <= MAX_COMMANDS, 'INVALID_INPUT')
    tables = {name: Table(t) for name, t in database.items()}
    views = {}
    revision = 0
    results = []
    for cmd in commands:
        try:
            op = shape_check(cmd)
        except DomainError as e:
            results.append(command_error(str(e)))
            continue
        if op == 'create':
            results.append(do_create(views, tables, cmd, revision))
        elif op == 'read':
            results.append(do_read(views, cmd, revision))
        elif op == 'drop':
            results.append(do_drop(views, cmd))
        else:
            r, revision = do_apply(tables, views, cmd, revision)
            results.append(r)
    return {'results': results}
