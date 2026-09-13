#!/usr/bin/env python3
import json
import sys
import copy
import re
from model import DomainError, validate, validate_request, fields, require
from parser import Parser
from binder import bind
from engine import optimize, execute


def command_mode(database, commands):
    # IDs and their positions are deliberately kept outside the SQL-visible rows.
    state = {}
    for name, table in database.items():
        ids = list(range(1, len(table['rows']) + 1))
        state[name] = {'ids': ids, 'used': set(ids)}
    views = {}
    revision = 0
    replies = []

    def compile_view(sql, do_opt):
        parsed = Parser(sql).parse()
        plan = bind(parsed, database)
        return optimize(plan) if do_opt else plan

    def install(name, sql, do_opt):
        plan = compile_view(sql, do_opt)
        views[name] = {'sql': sql, 'optimize': do_opt,
                       'tables': {n for n, _ in plan.query.sources},
                       'result': copy.deepcopy(execute(plan))}

    def command_shape(c):
        require(isinstance(c, dict), 'INVALID_COMMAND')
        op = c.get('op')
        if op == 'create': fields(c, {'op','view','sql','optimize'})
        elif op == 'read' or op == 'drop': fields(c, {'op','view'})
        elif op == 'apply': fields(c, {'op','changes'})
        else: raise DomainError('INVALID_COMMAND')
        if op in ('create','read','drop'):
            if op == 'create':
                require(isinstance(c['view'], str) and re.fullmatch(r'[a-z][a-z0-9-]{0,39}', c['view']) is not None, 'INVALID_COMMAND')
                require(isinstance(c['sql'], str) and type(c['optimize']) is bool, 'INVALID_COMMAND')
            else: require(isinstance(c['view'], str) and re.fullmatch(r'[a-z][a-z0-9-]{0,39}', c['view']) is not None, 'INVALID_COMMAND')
        else:
            changes = c['changes']
            require(isinstance(changes, list) and 0 < len(changes) <= 200, 'INVALID_COMMAND')
            for ch in changes:
                require(isinstance(ch, dict), 'INVALID_COMMAND')
                op2 = ch.get('op')
                expected = {'op','table','id','row'} if op2 in ('insert','update') else {'op','table','id'} if op2 == 'delete' else None
                require(expected is not None, 'INVALID_COMMAND')
                fields(ch, expected)
                require(isinstance(ch['table'], str) and re.fullmatch(r'[a-z_][a-z0-9_]*', ch['table']) is not None, 'INVALID_COMMAND')
                require(type(ch['id']) is int and 1 <= ch['id'] <= 2147483647, 'INVALID_COMMAND')
                if op2 in ('insert','update'): require(isinstance(ch['row'], list), 'INVALID_COMMAND')

    def check_row(table, row):
        require(len(row) == len(table['columns']), 'INVALID_ROW')
        for value, col in zip(row, table['columns']):
            good = value is None or type(value) is {'int':int,'text':str,'bool':bool}[col['type']]
            if good and col['type'] == 'int' and value is not None:
                good = -1000000000 <= value <= 1000000000
            require(good and (value is not None or col['nullable']), 'INVALID_ROW')

    for c in commands:
        try:
            command_shape(c)
            op = c['op']
            if op == 'create':
                if c['view'] in views: raise DomainError('VIEW_EXISTS')
                install(c['view'], c['sql'], c['optimize'])
                replies.append({'ok': True, 'result': {'view': c['view'], 'revision': revision}})
            elif op == 'read':
                if c['view'] not in views: raise DomainError('UNKNOWN_VIEW')
                replies.append({'ok': True, 'result': {'revision': revision, **copy.deepcopy(views[c['view']]['result'])}})
            elif op == 'drop':
                if c['view'] not in views: raise DomainError('UNKNOWN_VIEW')
                del views[c['view']]
                replies.append({'ok': True, 'result': {'dropped': c['view']}})
            else:
                # Work on a private copy. No view or revision is touched until every
                # operation, including repeated operations on one row, succeeds.
                newdb = copy.deepcopy(database)
                newstate = {n: {'ids': list(s['ids']), 'used': set(s['used'])} for n,s in state.items()}
                changed = set()
                for ch in c['changes']:
                    table_name, ident = ch['table'], ch['id']
                    if table_name not in newdb: raise DomainError('UNKNOWN_TABLE')
                    table, info = newdb[table_name], newstate[table_name]
                    if ch['op'] == 'insert':
                        check_row(table, ch['row'])
                        if ident in info['used']: raise DomainError('ROW_ID_USED')
                        info['used'].add(ident); info['ids'].append(ident); table['rows'].append(list(ch['row']))
                    else:
                        if ident not in info['ids']: raise DomainError('UNKNOWN_ROW')
                        pos = info['ids'].index(ident)
                        if ch['op'] == 'update':
                            check_row(table, ch['row']); table['rows'][pos] = list(ch['row'])
                        else:
                            info['ids'].pop(pos); table['rows'].pop(pos)
                    changed.add(table_name)
                database.clear(); database.update(newdb)
                state.clear(); state.update(newstate)
                revision += 1
                for view in views.values():
                    if view['tables'] & changed:
                        plan = compile_view(view['sql'], view['optimize'])
                        view['result'] = copy.deepcopy(execute(plan))
                replies.append({'ok': True, 'result': {'revision': revision}})
        except DomainError as e:
            replies.append({'ok': False, 'error': {'code': str(e)}})
    return {'ok': True, 'result': {'results': replies}}


def main():
    try:
        request = json.loads(sys.stdin.read())
        database, work = validate_request(request)
        if 'commands' in request['input']:
            response = command_mode(database, work)
        else:
            results = []
            for query in work:
                plan = bind(Parser(query['sql']).parse(), database)
                results.append(execute(optimize(plan) if query['optimize'] else plan))
            response = {'ok': True, 'result': {'results': results}}
    except DomainError as e:
        response = {'ok': False, 'error': {'code': str(e)}}
    except (ValueError, KeyError, TypeError):
        response = {'ok': False, 'error': {'code': 'INVALID_INPUT'}}
    print(json.dumps(response, separators=(',', ':')))

if __name__ == '__main__': main()
