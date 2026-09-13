#!/usr/bin/env python3
import json
import sys
import copy
import re
from model import DomainError, validate
from parser import Parser
from binder import bind
from engine import optimize, execute


VIEW_NAME = re.compile(r'[a-z][a-z0-9-]{0,39}\Z')


def command_fields(value, expected):
    if not isinstance(value, dict) or set(value) != expected:
        raise DomainError('INVALID_COMMAND')


def valid_id(value):
    return type(value) is int and 1 <= value <= 2147483647


def command_shape(command):
    """Validate only syntax here: semantic errors are per-command replies."""
    if not isinstance(command, dict) or not isinstance(command.get('op'), str):
        raise DomainError('INVALID_COMMAND')
    op = command['op']
    if op == 'create':
        command_fields(command, {'op', 'view', 'sql', 'optimize'})
        if not (isinstance(command['view'], str) and VIEW_NAME.fullmatch(command['view'])
                and isinstance(command['sql'], str) and type(command['optimize']) is bool):
            raise DomainError('INVALID_COMMAND')
    elif op in ('read', 'drop'):
        command_fields(command, {'op', 'view'})
        if not isinstance(command['view'], str) or not VIEW_NAME.fullmatch(command['view']):
            raise DomainError('INVALID_COMMAND')
    elif op == 'apply':
        command_fields(command, {'op', 'changes'})
        changes = command['changes']
        if not isinstance(changes, list) or not changes or len(changes) > 200:
            raise DomainError('INVALID_COMMAND')
        for change in changes:
            if not isinstance(change, dict) or change.get('op') not in ('insert', 'update', 'delete'):
                raise DomainError('INVALID_COMMAND')
            required = {'op', 'table', 'id'} | ({'row'} if change['op'] != 'delete' else set())
            command_fields(change, required)
            if not (isinstance(change['table'], str) and re.fullmatch(r'[a-z_][a-z0-9_]*', change['table'])
                    and valid_id(change['id']) and (change['op'] == 'delete' or isinstance(change['row'], list))):
                raise DomainError('INVALID_COMMAND')
    else:
        raise DomainError('INVALID_COMMAND')


def valid_row(table, row):
    columns = table['columns']
    if len(row) != len(columns):
        return False
    for value, column in zip(row, columns):
        if value is None:
            if not column['nullable']:
                return False
        elif type(value) is not {'int': int, 'text': str, 'bool': bool}[column['type']]:
            return False
        elif column['type'] == 'int' and abs(value) > 1000000000:
            return False
    return True


class View:
    def __init__(self, sql, optimize_flag, database):
        # The plan is retained: parsing and binding happen once per view lifetime.
        plan = bind(Parser(sql).parse(), database)
        self.plan = optimize(plan) if optimize_flag else plan
        self.tables = {name for name, _ in self.plan.query.sources}
        self.value = execute(self.plan)

    def refresh(self):
        # execute is a materialized-view refresh; reads never execute SQL.
        self.value = execute(self.plan)


def apply(database, metadata, views, changes, revision):
    # The private copy gives validation and view maintenance an all-or-nothing base.
    trial = copy.deepcopy(database)
    used = {name: set(info['used']) for name, info in metadata.items()}
    affected = set()
    for change in changes:
        name, ident = change['table'], change['id']
        if name not in trial:
            raise DomainError('UNKNOWN_TABLE')
        table = trial[name]
        if change['op'] != 'delete' and not valid_row(table, change['row']):
            raise DomainError('INVALID_ROW')
        positions = {rid: i for i, rid in enumerate(metadata[name]['live'])}
        if change['op'] == 'insert':
            if ident in used[name]:
                raise DomainError('ROW_ID_USED')
            table['rows'].append(copy.deepcopy(change['row']))
            used[name].add(ident)
            # Keep trial IDs alongside the copied tables, rather than exposing IDs to SQL.
            if '_trial_live' not in table: table['_trial_live'] = list(metadata[name]['live'])
            table['_trial_live'].append(ident)
        else:
            live = table.get('_trial_live', list(metadata[name]['live']))
            positions = {rid: i for i, rid in enumerate(live)}
            if ident not in positions:
                raise DomainError('UNKNOWN_ROW')
            pos = positions[ident]
            if change['op'] == 'update': table['rows'][pos] = copy.deepcopy(change['row'])
            else:
                del table['rows'][pos]
                del live[pos]
            table['_trial_live'] = live
        affected.add(name)
    # Commit base state and private ID state together.  Plans hold these table dicts.
    for name, table in database.items():
        table['rows'] = trial[name]['rows']
        metadata[name]['live'] = trial[name].get('_trial_live', list(metadata[name]['live']))
        metadata[name]['used'] = used[name]
    # Only dependent materializations are revisited; unrelated views retain cache.
    for view in views.values():
        if view.tables & affected:
            view.refresh()
    return revision + 1


def run_commands(database, commands):
    metadata = {name: {'live': list(range(1, len(t['rows']) + 1)),
                       'used': set(range(1, len(t['rows']) + 1))}
                for name, t in database.items()}
    views, replies, revision = {}, [], 0
    for command in commands:
        try:
            command_shape(command)
            op = command['op']
            if op == 'create':
                if command['view'] in views: raise DomainError('VIEW_EXISTS')
                view = View(command['sql'], command['optimize'], database)
                views[command['view']] = view
                value = {'view': command['view'], 'revision': revision}
            elif op == 'read':
                if command['view'] not in views: raise DomainError('UNKNOWN_VIEW')
                cached = views[command['view']].value
                value = {'revision': revision, 'columns': list(cached['columns']),
                         'rows': copy.deepcopy(cached['rows'])}
            elif op == 'drop':
                if command['view'] not in views: raise DomainError('UNKNOWN_VIEW')
                del views[command['view']]
                value = {'dropped': command['view']}
            else:
                revision = apply(database, metadata, views, command['changes'], revision)
                value = {'revision': revision}
            replies.append({'ok': True, 'result': value})
        except DomainError as error:
            replies.append({'ok': False, 'error': {'code': str(error)}})
    return {'results': replies}


def main():
    try:
        request = json.loads(sys.stdin.read())
        database, queries, commands = validate(request)
        if commands is not None:
            response = {'ok': True, 'result': run_commands(database, commands)}
        else:
            results = []
            for query in queries:
                plan = bind(Parser(query['sql']).parse(), database)
                results.append(execute(optimize(plan) if query['optimize'] else plan))
            response = {'ok': True, 'result': {'results': results}}
    except DomainError as e:
        response = {'ok': False, 'error': {'code': str(e)}}
    except (ValueError, KeyError, TypeError):
        response = {'ok': False, 'error': {'code': 'INVALID_INPUT'}}
    print(json.dumps(response, separators=(',', ':')))

if __name__ == '__main__': main()
