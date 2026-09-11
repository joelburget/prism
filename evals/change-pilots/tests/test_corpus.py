"""Sanity checks on hand-authored expected results, not task implementations.

These checks trust fixture acceptance/rejection decisions. They cannot run a task
or certify its oracle, but catch inconsistent receipts, arithmetic, and traces.
"""

from collections import Counter
import unittest

from test_harness import runner


class CorpusConsistencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = runner.load_cases()

    def successful(self, task):
        return [case for case in self.cases if case.task == task and case.expect["ok"]]

    def test_query_pairs_and_result_shapes(self):
        for case in self.successful("query-null"):
            with self.subTest(case=case.id):
                queries = case.input["queries"]
                results = case.expect["result"]["results"]
                self.assertEqual(len(queries), len(results))
                self.assertEqual(len(queries) % 2, 0)
                for index in range(0, len(queries), 2):
                    self.assertEqual(queries[index]["sql"], queries[index + 1]["sql"])
                    self.assertIs(queries[index]["optimize"], False)
                    self.assertIs(queries[index + 1]["optimize"], True)
                    self.assertIsNone(runner.first_difference(results[index], results[index + 1]))
                for result in results:
                    self.assertEqual(set(result), {"columns", "rows"})
                    self.assertEqual(len(result["columns"]), len(set(result["columns"])))
                    for row in result["rows"]:
                        self.assertEqual(len(row), len(result["columns"]))

    def test_workflow_service_audits_explain_effects(self):
        for case in self.successful("workflow-recovery"):
            with self.subTest(case=case.id):
                definitions = {step["id"]: step for step in case.input["steps"]}
                final = case.expect["result"]["final"]
                run_ids = {run["id"] for run in final["runs"]}
                failures = Counter()
                applied = set()
                effects = []
                for call in final["calls"]:
                    self.assertEqual(set(call), {"kind", "key", "attempt", "outcome"})
                    key = tuple(call["key"])
                    self.assertEqual(len(key), 2)
                    self.assertIn(key[0], run_ids)
                    self.assertIn(key[1], definitions)
                    self.assertGreaterEqual(call["attempt"], 1)
                    self.assertLessEqual(call["attempt"], case.input.get("max_attempts", 3))
                    if call["kind"] == "lookup":
                        self.assertEqual(call["outcome"], "found" if key in applied else "missing")
                    else:
                        self.assertEqual(call["kind"], "execute")
                        if key in applied:
                            self.assertEqual(call["outcome"], "replayed")
                        elif failures[key] < definitions[key[1]].get("failures", 0):
                            self.assertEqual(call["outcome"], "transient")
                            failures[key] += 1
                        else:
                            self.assertEqual(call["outcome"], "applied")
                            applied.add(key)
                            effects.append({"key": list(key), "amount": definitions[key[1]]["amount"]})
                self.assertEqual(final["effects"], effects)
                for run in final["runs"]:
                    for step in run["steps"]:
                        if step["status"] == "succeeded":
                            self.assertIn((run["id"], step["id"]), applied)

    def test_workflow_snapshots_are_consistent(self):
        for case in self.successful("workflow-recovery"):
            with self.subTest(case=case.id):
                result = case.expect["result"]
                self.assertEqual(len(result["observations"]),
                                 sum(command["op"] == "observe" for command in case.input["commands"]))
                definitions = case.input["steps"]
                previous_time = 0
                for snapshot in [*result["observations"], result["final"]]:
                    self.assertGreaterEqual(snapshot["now"], previous_time)
                    previous_time = snapshot["now"]
                    for run in snapshot["runs"]:
                        steps = {step["id"]: step for step in run["steps"]}
                        self.assertEqual([step["id"] for step in run["steps"]],
                                         [step["id"] for step in definitions])
                        for definition in definitions:
                            step = steps[definition["id"]]
                            self.assertGreaterEqual(step["attempts"], 0)
                            self.assertLessEqual(step["attempts"], case.input.get("max_attempts", 3))
                            if step["status"] == "succeeded":
                                for dependency in definition["needs"]:
                                    self.assertEqual(steps[dependency]["status"], "succeeded")
                        statuses = {step["status"] for step in run["steps"]}
                        if run["status"] == "succeeded":
                            self.assertEqual(statuses, {"succeeded"})
                        elif run["status"] == "failed":
                            self.assertIn("failed", statuses)
                            self.assertFalse(statuses & {"running", "pending"})
                        elif run["status"] == "cancelled":
                            self.assertTrue(run["cancel_requested"])
                            self.assertFalse(statuses & {"running", "pending"})

    def test_ledger_receipts_audits_and_report_arithmetic(self):
        for case in self.successful("ledger-refunds"):
            with self.subTest(case=case.id):
                operations = case.input["operations"]
                results = case.expect["result"]["results"]
                self.assertEqual(len(operations), len(results))
                events = []
                keys = {}
                for operation, response in zip(operations, results):
                    runner.response_contract(response)
                    if not response["ok"]:
                        continue  # Legality and error selection require independent review.
                    result = response["result"]
                    kind = operation["op"]
                    if kind in {"invoice", "payment", "close", "refund", "reverse"}:
                        self.assertEqual(set(result), {"event_id", "replayed"})
                        if result["replayed"]:
                            self.assertEqual(keys[operation["key"]], (operation, result["event_id"]))
                        else:
                            self.assertNotIn(operation["key"], keys)
                            self.assertEqual(result["event_id"], len(events) + 1)
                            keys[operation["key"]] = (operation, result["event_id"])
                            events.append({"event_id": result["event_id"], "operation": operation})
                    elif kind == "audit":
                        prefix = events[:operation.get("as_of", len(events))]
                        self.assertEqual(result, {"events": prefix})
                    else:
                        self.assertEqual(kind, "report")
                        prefix = events[:operation.get("as_of", len(events))]
                        self.assert_report_projection(prefix, result)

    def assert_report_projection(self, prefix, report):
        # Project fixture-accepted events as signed contributions. This does not
        # decide whether any payment/refund/reversal should have been accepted.
        invoices = {}
        contributions = {}
        closed = 0
        for event in prefix:
            operation = event["operation"]
            kind = operation["op"]
            contribution = []
            if kind == "invoice":
                invoices[operation["invoice"]] = {"invoice": operation["invoice"],
                                                   "total": operation["total"], "paid": 0, "refunded": 0}
            elif kind == "close":
                closed = operation["through"]
            elif kind in {"payment", "refund"}:
                for allocation in operation["allocations"]:
                    amount = allocation["amount"]
                    contribution.append((allocation["invoice"], amount if kind == "payment" else -amount,
                                         0 if kind == "payment" else amount))
            elif kind == "reverse":
                contribution = [(invoice, -paid, -refunded)
                                for invoice, paid, refunded in contributions[operation["target"]]]
            contributions[event["event_id"]] = contribution
            for invoice, paid, refunded in contribution:
                invoices[invoice]["paid"] += paid
                invoices[invoice]["refunded"] += refunded
        rows = []
        for name in sorted(invoices):
            row = invoices[name]
            self.assertGreaterEqual(row["paid"], 0)
            self.assertLessEqual(row["paid"], row["total"])
            self.assertGreaterEqual(row["refunded"], 0)
            row["due"] = row["total"] - row["paid"]
            row["status"] = "unpaid" if row["paid"] == 0 else "paid" if row["due"] == 0 else "partially_paid"
            rows.append(row)
        expected = {"closed_through": closed, "invoices": rows, "cash": sum(row["paid"] for row in rows),
                    "outstanding": sum(row["due"] for row in rows)}
        self.assertIsNone(runner.first_difference(expected, report))


if __name__ == "__main__":
    unittest.main()
