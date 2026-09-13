#!/usr/bin/env python3
import json
import sys
from model import DomainError, validate
from parser import Parser
from binder import bind
from engine import optimize, execute
from viewdb import run_commands


def main():
    try:
        request = json.loads(sys.stdin.read())
        mode, database, payload = validate(request)
        if mode == 'queries':
            results = []
            for query in payload:
                plan = bind(Parser(query['sql']).parse(), database)
                results.append(execute(optimize(plan) if query['optimize'] else plan))
            response = {'ok': True, 'result': {'results': results}}
        else:
            response = {'ok': True, 'result': run_commands(database, payload)}
    except DomainError as e:
        response = {'ok': False, 'error': {'code': str(e)}}
    except (ValueError, KeyError, TypeError):
        response = {'ok': False, 'error': {'code': 'INVALID_INPUT'}}
    print(json.dumps(response, separators=(',', ':')))

if __name__ == '__main__': main()
