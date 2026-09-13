"""Syntax and bound-plan data shared by the independent compiler stages."""
from dataclasses import dataclass, field
import re

class DomainError(Exception):
    pass

def require(condition, code='INVALID_INPUT'):
    if not condition:
        raise DomainError(code)

RESERVED = set('SELECT DISTINCT AS FROM INNER JOIN LEFT OUTER ON WHERE GROUP BY HAVING ORDER ASC DESC NULLS FIRST LAST LIMIT OFFSET AND OR NOT IS NULL TRUE FALSE COALESCE COUNT SUM MIN MAX'.split())

# 'null' is the type of an untyped NULL literal; it unifies with any concrete type.
TYPES = ('int', 'text', 'bool')

def identifier(s):
    return isinstance(s, str) and re.fullmatch('[a-z_][a-z0-9_]*', s) is not None and s.upper() not in RESERVED

@dataclass
class Expr:
    op: str
    value: object = None
    args: list = field(default_factory=list)
    type: str = ''
    index: int = -1
    source: int = -1
    nullable: bool = True

@dataclass
class Join:
    """One join step; `outer` marks LEFT [OUTER] JOIN."""
    on: Expr
    outer: bool = False

@dataclass
class Order:
    index: int
    descending: bool = False
    nulls_first: bool | None = None

@dataclass
class Query:
    select: list = field(default_factory=list)
    sources: list = field(default_factory=list)
    joins: list = field(default_factory=list)
    where: Expr | None = None
    groups: list = field(default_factory=list)
    having: Expr | None = None
    order: list = field(default_factory=list)
    distinct: bool = False
    limit: int | None = None
    offset: int = 0

@dataclass
class Plan:
    query: Query
    tables: list
    filters: list
    aggregate: bool


def validate(request):
    require(isinstance(request, dict) and request.get('protocol_version') == 1 and type(request.get('protocol_version')) is int and request.get('task') == 'query-null')
    data = request.get('input')
    fields(data, {'database', 'queries'})
    require(isinstance(data['database'], list) and isinstance(data['queries'], list))
    database = {}
    for t in data['database']:
        fields(t, {'name', 'columns', 'rows'})
        require(identifier(t['name']) and t['name'] not in database)
        require(isinstance(t['columns'], list) and len(t['columns']) > 0 and isinstance(t['rows'], list))
        names = set()
        for c in t['columns']:
            fields(c, {'name', 'type', 'nullable'})
            require(identifier(c['name']) and c['name'] not in names and c['type'] in TYPES and type(c['nullable']) is bool)
            names.add(c['name'])
        for row in t['rows']:
            require(isinstance(row, list) and len(row) == len(t['columns']))
            for v, c in zip(row, t['columns']):
                if v is None:
                    require(c['nullable'])
                else:
                    require(type(v) is {'int': int, 'text': str, 'bool': bool}[c['type']])
        database[t['name']] = t
    for q in data['queries']:
        fields(q, {'sql', 'optimize'})
        require(isinstance(q['sql'], str) and type(q['optimize']) is bool)
    return database, data['queries']


def fields(value, expected):
    require(isinstance(value, dict) and set(value) == expected)
