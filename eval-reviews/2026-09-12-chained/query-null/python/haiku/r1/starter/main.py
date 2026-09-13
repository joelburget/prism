#!/usr/bin/env python3
import json
import sys
from model import DomainError, validate
from parser import Parser
from binder import bind
from engine import optimize, execute
from views import ViewManager


def main():
    try:
        request = json.loads(sys.stdin.read())
        database, queries, commands = validate(request)
        
        if queries is not None:
            # Old protocol: queries
            results = []
            for query in queries:
                plan = bind(Parser(query['sql']).parse(), database)
                results.append(execute(optimize(plan) if query['optimize'] else plan))
            response = {'ok': True, 'result': {'results': results}}
        else:
            # New protocol: commands
            manager = ViewManager(database)
            results = []
            
            for cmd in commands:
                try:
                    op = cmd.get('op')
                    
                    if op == 'create':
                        view = cmd.get('view')
                        sql = cmd.get('sql')
                        optimize_flag = cmd.get('optimize')
                        
                        # Validate command structure
                        if not isinstance(view, str) or not isinstance(sql, str) or not isinstance(optimize_flag, bool):
                            raise DomainError('INVALID_COMMAND')
                        
                        # Check for unknown fields
                        expected_fields = {'op', 'view', 'sql', 'optimize'}
                        if set(cmd.keys()) != expected_fields:
                            raise DomainError('INVALID_COMMAND')
                        
                        view_name, revision = manager.create_view(view, sql, optimize_flag)
                        results.append({
                            'ok': True,
                            'result': {
                                'view': view_name,
                                'revision': revision
                            }
                        })
                    
                    elif op == 'read':
                        view = cmd.get('view')
                        
                        if not isinstance(view, str):
                            raise DomainError('INVALID_COMMAND')
                        
                        expected_fields = {'op', 'view'}
                        if set(cmd.keys()) != expected_fields:
                            raise DomainError('INVALID_COMMAND')
                        
                        result = manager.read_view(view)
                        results.append({
                            'ok': True,
                            'result': result
                        })
                    
                    elif op == 'drop':
                        view = cmd.get('view')
                        
                        if not isinstance(view, str):
                            raise DomainError('INVALID_COMMAND')
                        
                        expected_fields = {'op', 'view'}
                        if set(cmd.keys()) != expected_fields:
                            raise DomainError('INVALID_COMMAND')
                        
                        dropped = manager.drop_view(view)
                        results.append({
                            'ok': True,
                            'result': {
                                'dropped': dropped
                            }
                        })
                    
                    elif op == 'apply':
                        changes = cmd.get('changes')
                        
                        if not isinstance(changes, list) or len(changes) == 0:
                            raise DomainError('INVALID_COMMAND')
                        
                        if len(changes) > 200:
                            raise DomainError('INVALID_COMMAND')
                        
                        expected_fields = {'op', 'changes'}
                        if set(cmd.keys()) != expected_fields:
                            raise DomainError('INVALID_COMMAND')
                        
                        revision = manager.apply_changes(changes)
                        results.append({
                            'ok': True,
                            'result': {
                                'revision': revision
                            }
                        })
                    
                    else:
                        raise DomainError('INVALID_COMMAND')
                
                except DomainError as e:
                    results.append({
                        'ok': False,
                        'error': {
                            'code': str(e)
                        }
                    })
            
            response = {'ok': True, 'result': {'results': results}}
    
    except DomainError as e:
        response = {'ok': False, 'error': {'code': str(e)}}
    except (ValueError, KeyError, TypeError) as e:
        response = {'ok': False, 'error': {'code': 'INVALID_INPUT'}}
    
    print(json.dumps(response, separators=(',', ':')))


if __name__ == '__main__': main()
