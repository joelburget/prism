#!/usr/bin/env python3
import json
import sys
from model import DomainError, validate
from parser import Parser
from binder import bind
from engine import optimize, execute
from session import Session


def main():
    try:
        request = json.loads(sys.stdin.read())
        database, mode, items = validate(request)
        if mode == 'queries':
            results = []
            for query in items:
                plan = bind(Parser(query['sql']).parse(), database)
                results.append(execute(optimize(plan) if query['optimize'] else plan))
        else:
            results = Session(database).run(items)
        response = {'ok': True, 'result': {'results': results}}
    except DomainError as e:
        response = {'ok': False, 'error': {'code': str(e)}}
    except (ValueError, KeyError, TypeError):
        response = {'ok': False, 'error': {'code': 'INVALID_INPUT'}}
    print(json.dumps(response, separators=(',', ':')))

if __name__ == '__main__': main()
