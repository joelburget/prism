#!/usr/bin/env python3
import json
import sys
from model import DomainError, validate
from parser import Parser
from binder import bind
from engine import optimize, execute


def main():
    try:
        request = json.loads(sys.stdin.read())
        database, queries = validate(request)
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
