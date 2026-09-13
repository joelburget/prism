#!/usr/bin/env python3
import json
import sys
import copy
import re
from model import DomainError, validate, require
from parser import Parser
from binder import bind
from engine import optimize, execute
from views import MaterializedView


VIEW_NAME = re.compile(r'[a-z][a-z0-9-]{0,39}')
TABLE_NAME = re.compile(r'[a-z_][a-z0-9_]*')


def reply_error(code):
    return {'ok': False, 'error': {'code': code}}


def reply(value):
    return {'ok': True, 'result': value}


def command_shape(command):
    require(isinstance(command, dict) and isinstance(command.get('op'), str), 'INVALID_COMMAND')
    op = command['op']
    expected = {
        'create': {'op', 'view', 'sql', 'optimize'},
        'read': {'op', 'view'},
        'drop': {'op', 'view'},
        'apply': {'op', 'changes'},
    }
    require(op in expected and set(command) == expected[op], 'INVALID_COMMAND')
    if op in ('create', 'read', 'drop'):
        require(isinstance(command['view'], str) and VIEW_NAME.fullmatch(command['view']), 'INVALID_COMMAND')
    if op == 'create':
        require(isinstance(command['sql'], str) and type(command['optimize']) is bool, 'INVALID_COMMAND')
    if op == 'apply':
        changes = command['changes']
        require(isinstance(changes, list) and 1 <= len(changes) <= 200, 'INVALID_COMMAND')
        for change in changes:
            require(isinstance(change, dict) and isinstance(change.get('op'), str), 'INVALID_COMMAND')
            fields = {'insert': {'op', 'table', 'id', 'row'},
                      'update': {'op', 'table', 'id', 'row'},
                      'delete': {'op', 'table', 'id'}}
            cop = change['op']
            require(cop in fields and set(change) == fields[cop], 'INVALID_COMMAND')
            require(isinstance(change['table'], str) and TABLE_NAME.fullmatch(change['table']), 'INVALID_COMMAND')
            require(type(change['id']) is int and 1 <= change['id'] <= 2147483647, 'INVALID_COMMAND')
            if cop in ('insert', 'update'): require(isinstance(change['row'], list), 'INVALID_COMMAND')


def valid_row(table, row):
    if len(row) != len(table['columns']): return False
    for value, column in zip(row, table['columns']):
        if value is None:
            if not column['nullable']: return False
        elif type(value) is not {'int': int, 'text': str, 'bool': bool}[column['type']]:
            return False
        if type(value) is int and not -1000000000 <= value <= 1000000000: return False
    return True


def apply_batch(database, views, changes, revision):
    # Shape validation deliberately precedes every table/row-state check.
    command_shape({'op': 'apply', 'changes': changes})
    new_database = dict(database)
    copied_tables = set()
    touched = {}
    for change in changes:
        name, row_id, op = change['table'], change['id'], change['op']
        require(name in new_database, 'UNKNOWN_TABLE')
        if name not in copied_tables:
            new_database[name] = copy.deepcopy(new_database[name])
            copied_tables.add(name)
        table = new_database[name]
        if op in ('insert', 'update'):
            require(valid_row(table, change['row']), 'INVALID_ROW')
        if op == 'insert':
            require(row_id not in table['_used_ids'], 'ROW_ID_USED')
            table['_used_ids'].add(row_id)
            table['_rows_by_id'][row_id] = list(change['row'])
        else:
            require(row_id in table['_rows_by_id'], 'UNKNOWN_ROW')
            if op == 'update': table['_rows_by_id'][row_id] = list(change['row'])
            else: del table['_rows_by_id'][row_id]
        touched.setdefault(name, set()).add(row_id)
    for name in copied_tables:
        table = new_database[name]
        table['rows'] = list(table['_rows_by_id'].values())
    # No view is touched until the complete private base-table batch validates.
    # Maintenance itself has no domain-error path, so these become the committed
    # states together before the command reply is exposed.
    for view in views.values():
        if any(name in touched for name, _ in view.plan.query.sources):
            view.apply(new_database, touched)
        else:
            view._attach(new_database)
    return new_database, views, revision + 1


def run_commands(database, commands):
    for table in database.values():
        table['_rows_by_id'] = {i: row for i, row in enumerate(table['rows'], 1)}
        table['_used_ids'] = set(table['_rows_by_id'])
    views, revision, results = {}, 0, []
    for command in commands:
        try:
            command_shape(command)
            op = command['op']
            if op == 'create':
                require(command['view'] not in views, 'VIEW_EXISTS')
                plan = bind(Parser(command['sql']).parse(), database)
                if command['optimize']: plan = optimize(plan)
                views[command['view']] = MaterializedView(plan, database)
                value = {'view': command['view'], 'revision': revision}
            elif op == 'read':
                require(command['view'] in views, 'UNKNOWN_VIEW')
                value = {'revision': revision, **views[command['view']].read()}
            elif op == 'drop':
                require(command['view'] in views, 'UNKNOWN_VIEW')
                del views[command['view']]
                value = {'dropped': command['view']}
            else:
                database, views, revision = apply_batch(database, views, command['changes'], revision)
                value = {'revision': revision}
            results.append(reply(value))
        except DomainError as error:
            results.append(reply_error(str(error)))
    return results


def main():
    try:
        def unique_object(pairs):
            value = {}
            for key, item in pairs:
                if key in value: raise DomainError('INVALID_INPUT')
                value[key] = item
            return value
        request = json.loads(sys.stdin.read(), object_pairs_hook=unique_object)
        database, mode, operations = validate(request)
        if mode == 'commands':
            results = run_commands(database, operations)
        else:
            results = []
            for query in operations:
                plan = bind(Parser(query['sql']).parse(), database)
                results.append(execute(optimize(plan) if query['optimize'] else plan))
        response = {'ok': True, 'result': {'results': results}}
    except DomainError as e:
        response = {'ok': False, 'error': {'code': str(e)}}
    except (ValueError, KeyError, TypeError):
        response = {'ok': False, 'error': {'code': 'INVALID_INPUT'}}
    print(json.dumps(response, separators=(',', ':')))

if __name__ == '__main__': main()
