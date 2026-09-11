"""Two focused fault witnesses: new cases expose gaps in the public corpus.

These are narrow fault models, not complete faulty implementations of the tasks.
They establish a specific added distinction, not a mutation score for the suite.
"""

from contextlib import closing
from pathlib import Path
import re
import sqlite3
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tests'))
from test_harness import runner


class DiscriminationTests(unittest.TestCase):
    def test_wrong_sum_fallback_rewrite_is_unexercised_publicly_but_fails_heldout(self):
        # Fault: move a per-row fallback outside SUM, applying it once per group.
        pattern = re.compile(r'SUM\(COALESCE\(([a-z_][a-z0-9_]*),\s*(\d+)\)\)', re.I)
        public = runner.load_cases(ROOT, ['query-null'])
        self.assertFalse(any(pattern.search(query['sql']) for case in public for query in case.input['queries']))
        case = next(case for case in runner.load_cases(ROOT / 'heldout', ['query-null'])
                    if case.id == 'heldout-coalesce-inside-versus-outside-sum')
        sql = case.input['queries'][0]['sql']
        faulty = pattern.sub(r'COALESCE(SUM(\1), \2)', sql)
        self.assertNotEqual(sql, faulty)
        with closing(sqlite3.connect(':memory:')) as database:
            for table in case.input['database']:
                columns = ', '.join(f'"{column["name"]}" INTEGER' for column in table['columns'])
                database.execute(f'CREATE TABLE "{table["name"]}" ({columns})')
                placeholders = ','.join('?' for _ in table['columns'])
                database.executemany(f'INSERT INTO "{table["name"]}" VALUES ({placeholders})', table['rows'])
            correct_rows = sorted(database.execute(sql).fetchall())
            faulty_rows = sorted(database.execute(faulty).fetchall())
        # Only compare values here: the separate fixture checks retain the
        # published encounter-order contract, which SQLite does not guarantee.
        expected = sorted(tuple(row) for row in case.expect['result']['results'][0]['rows'])
        self.assertEqual(correct_rows, expected)
        self.assertNotEqual(faulty_rows, expected)

    @staticmethod
    def narrow_refund_sum_witnesses(cases):
        affected = set()
        for case in cases:
            if not case.expect['ok']:
                continue
            events = []
            for operation, response in zip(case.input['operations'], case.expect['result']['results']):
                if not response['ok'] or operation['op'] == 'audit':
                    continue
                if operation['op'] == 'report':
                    prefix = events[:operation.get('as_of', len(events))]
                    reversed_ids = {event['target'] for event in prefix if event['op'] == 'reverse'}
                    correction = 0
                    for event_id, event in enumerate(prefix, 1):
                        if event['op'] == 'refund' and event_id not in reversed_ids:
                            total = sum(allocation['amount'] for allocation in event['allocations'])
                            signed32 = (total + 2**31) % 2**32 - 2**31
                            correction += total - signed32
                    if correction:
                        # The faulty refund accumulator changes an observed cash
                        # value even if payment accumulation uses wide integers.
                        affected.add(case.id)
                    continue
                receipt = response['result']
                if receipt['replayed']:
                    continue
                events.append(operation)
        return affected

    def test_narrow_refund_sum_misses_public_but_changes_heldout_cash(self):
        # Fault: a signed 32-bit accumulator in the new refund code, while
        # baseline payment totals and the other behavior remain correct.
        public = runner.load_cases(ROOT, ['ledger-refunds'])
        heldout = runner.load_cases(ROOT / 'heldout', ['ledger-refunds'])
        self.assertEqual(self.narrow_refund_sum_witnesses(public), set())
        affected = self.narrow_refund_sum_witnesses(heldout)
        self.assertIn('heldout-large-aggregate-refund-and-reversal', affected)


if __name__ == '__main__':
    unittest.main()
