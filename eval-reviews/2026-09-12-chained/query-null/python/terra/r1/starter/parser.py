"""SQL tokenizer and recursive precedence parser; no database access."""
import re
from model import Expr, Query, DomainError, RESERVED, identifier, require

TOKEN = re.compile(r"\s+|'(?:[^']|'')*'|[a-zA-Z_][a-zA-Z0-9_]*|[0-9]+|<>|<=|>=|[(),.;+*=<>-]")
PRECEDENCE = {'OR': 1, 'AND': 2, '=': 4, '<>': 4, '<': 4, '>': 4, '<=': 4, '>=': 4, '+': 5, '-': 5, '*': 6}
AGGREGATES = {'COUNT', 'SUM', 'MIN', 'MAX'}

class Parser:
    def __init__(self, sql):
        self.tokens = []
        i = 0
        while i < len(sql):
            m = TOKEN.match(sql, i)
            require(m is not None, 'PARSE_ERROR')
            t = m.group()
            i = m.end()
            if not t.isspace():
                self.tokens.append(t.upper() if t.upper() in RESERVED else t)
        self.tokens.append('$END')
        self.i = 0

    def peek(self): return self.tokens[self.i]
    def pop(self):
        t = self.peek()
        require(t != '$END', 'PARSE_ERROR')
        self.i += 1
        return t
    def take(self, t):
        if self.peek() == t:
            self.i += 1
            return True
        return False
    def need(self, t): require(self.take(t), 'PARSE_ERROR')
    def name(self):
        t = self.pop()
        require(identifier(t), 'PARSE_ERROR')
        return t

    def expr(self, minimum=1):
        t = self.pop()
        if t == 'NOT': left = Expr('NOT', args=[self.expr(3)])
        elif t == '(':
            left = self.expr()
            self.need(')')
        elif t == '-':
            n = self.pop()
            require(n.isascii() and n.isdigit(), 'PARSE_ERROR')
            left = Expr('lit', -int(n), type='int')
        elif t.isdigit(): left = Expr('lit', int(t), type='int')
        elif t.startswith("'"): left = Expr('lit', t[1:-1].replace("''", "'"), type='text')
        elif t in ('TRUE', 'FALSE'): left = Expr('lit', t == 'TRUE', type='bool')
        elif t == 'NULL': left = Expr('lit', None, type='null')
        elif t == 'COALESCE':
            self.need('(')
            args = [self.expr()]
            while self.take(','): args.append(self.expr())
            require(len(args) >= 2, 'PARSE_ERROR')
            self.need(')')
            left = Expr('COALESCE', args=args)
        elif t in AGGREGATES:
            self.need('(')
            if t == 'COUNT' and self.take('*'): left = Expr(t)
            else: left = Expr(t, args=[self.expr()])
            self.need(')')
        else:
            require(identifier(t), 'PARSE_ERROR')
            left = Expr('col', (t, self.name()) if self.take('.') else ('', t))
        compared = False
        while True:
            if self.peek() == 'IS' and minimum <= 4:
                require(not compared, 'PARSE_ERROR')
                self.pop()
                negated = self.take('NOT')
                self.need('NULL')
                left = Expr('IS NOT NULL' if negated else 'IS NULL', args=[left])
                compared = True
                continue
            if PRECEDENCE.get(self.peek(), 0) < minimum: break
            op = self.pop()
            p = PRECEDENCE[op]
            require(not (p == 4 and compared), 'PARSE_ERROR')
            right = self.expr(p + 1)
            left = Expr(op, args=[left, right])
            if p == 4: compared = True
        return left

    def source(self):
        name = self.name()
        return (name, self.name() if self.take('AS') else name)

    def parse(self):
        q = Query()
        self.need('SELECT')
        q.distinct = self.take('DISTINCT')
        while True:
            e = self.expr()
            self.need('AS')
            q.select.append((e, self.name()))
            if not self.take(','): break
        self.need('FROM')
        q.sources.append(self.source())
        while self.peek() in ('INNER', 'JOIN', 'LEFT'):
            left = self.take('LEFT')
            if left: self.take('OUTER')
            else: self.take('INNER')
            self.need('JOIN')
            q.sources.append(self.source())
            self.need('ON')
            q.joins.append(('LEFT' if left else 'INNER', self.expr()))
        if self.take('WHERE'): q.where = self.expr()
        if self.take('GROUP'):
            self.need('BY')
            while True:
                q.groups.append(Expr('col', self.reference()))
                if not self.take(','): break
        if self.take('HAVING'): q.having = self.expr()
        if self.take('ORDER'):
            self.need('BY')
            while True:
                alias = self.name()
                desc = self.take('DESC')
                if not desc: self.take('ASC')
                nulls = None
                if self.take('NULLS'):
                    if self.take('FIRST'): nulls = 'FIRST'
                    elif self.take('LAST'): nulls = 'LAST'
                    else: raise DomainError('PARSE_ERROR')
                q.order.append((alias, desc, nulls))
                if not self.take(','): break
        if self.take('LIMIT'):
            q.limit = self.natural()
            if self.take('OFFSET'): q.offset = self.natural()
        self.take(';')
        require(self.peek() == '$END', 'PARSE_ERROR')
        return q

    def natural(self):
        t = self.pop()
        require(t.isdigit(), 'PARSE_ERROR')
        return int(t)

    def reference(self):
        t = self.name()
        return (t, self.name()) if self.take('.') else ('', t)
