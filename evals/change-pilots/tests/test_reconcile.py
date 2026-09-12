"""Reconciliation preserves measurements and never calls or retries a model."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_chained_batch
from experiments import chained_batch as chain
from experiments import native_batch as native
from experiments import reconcile
from experiments.control import tree_fingerprint


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        test_chained_batch.ChainedBatchTests.setUp(self)
        self.old_inputs = {'original': {'experiments/native_bridge.py': 'before'}, 'followups': {'fixed': True}}
        self.fingerprints = self.stack.enter_context(patch.object(chain, 'fingerprints', return_value=self.old_inputs))
        self.regrader = self.stack.enter_context(patch.object(reconcile, 'grade_submission', return_value=self.scores))
        self.output = self.root.with_name('continuation')
        original_execute = native.DockerSandbox.execute
        def execute(sandbox, command, timeout_seconds):
            if '\0' in command:
                raise ValueError('embedded null byte')
            return original_execute(sandbox, command, timeout_seconds)
        self.stack.enter_context(patch.object(native.DockerSandbox, 'execute', execute))

    def interrupted(self, repetitions=1):
        plan = chain.make_plan(self.root, ['luna'], ['query-null'], ['python'], repetitions=repetitions)
        original_agent = self.agent.side_effect
        calls = []
        def agent(client, argv, prompt, execute, record, **limits):
            calls.append(True)
            if len(calls) < 2 * repetitions - 1:
                return original_agent(client, argv, prompt, execute, record, **limits)
            record({'event': 'native_event', 'payload': {'type': 'assistant', 'message': {'model': 'gpt-5.6-luna'}}})
            record({'event': 'tool_call', 'call_id': 1, 'command': 'echo a\0b', 'timeout_seconds': 30})
            with self.assertRaises(ValueError): execute('echo a\0b', 30)
            record({'event': 'tool_result', 'call_id': 1, 'result': {'isError': True,
                'content': [{'type': 'text', 'text': 'Isolated execute failed: ValueError'}]}})
            record({'event': 'tool_call', 'call_id': 2, 'command': 'echo corrected', 'timeout_seconds': 30})
            execute('echo corrected', 30)
            record({'event': 'tool_result', 'call_id': 2, 'result': {'isError': False, 'content': []}})
            return {**self.agent_result, 'exit_code': 0}
        self.agent.side_effect = agent
        result = chain.execute_plan(self.root)
        ident = result['stopped_on_infrastructure_run']
        self.assertIsNotNone(ident)
        self.fingerprints.return_value = {'original': {'experiments/native_bridge.py': 'after',
            'experiments/reconcile.py': 'added'}, 'followups': {'fixed': True}}
        return plan, ident

    def test_regrade_frozen_source_then_continue_only_unstarted_child(self):
        plan, ident = self.interrupted()
        original = self.root/'runs'/ident
        digest = tree_fingerprint(original)
        result_bytes = (original/'result.json').read_bytes()
        calls = self.agent.call_count
        result = reconcile.continue_nul_error(self.root, self.output, ident)
        self.assertEqual(self.agent.call_count, calls)
        self.assertEqual(result['remaining_stages'], 1)
        self.assertEqual(tree_fingerprint(original), digest)
        copied = self.output/'runs'/ident
        self.assertEqual((copied/'original-result.json').read_bytes(), result_bytes)
        self.assertEqual((copied/'metadata.json').read_bytes(), (original/'metadata.json').read_bytes())
        reconciled = json.loads((copied/'result.json').read_text())
        self.assertTrue(reconciled['success'])
        self.assertEqual(reconciled['elapsed_seconds'], json.loads(result_bytes)['elapsed_seconds'])
        self.assertEqual(self.regrader.call_args.args[1], (copied/'source').resolve())
        self.assertEqual(json.loads((self.output/'plan.json').read_text())['runs'], plan['runs'])
        self.agent.side_effect = lambda *a, **kw: dict(self.agent_result)
        self.assertEqual(chain.execute_plan(self.output)['runs_finished'], 1)
        self.assertEqual(self.agent.call_count, calls + 1)
        self.assertEqual(tree_fingerprint(original), digest)

    def test_completed_prefix_copied_byte_for_byte_including_failures(self):
        self.scores['heldout']['passed'] = False
        self.scores['heldout']['cases'][0]['status'] = 'fail'
        plan, ident = self.interrupted(repetitions=2)
        digests = {c['run_id']: tree_fingerprint(self.root/'runs'/c['run_id']) for c in plan['runs'][:2]}
        result = reconcile.continue_nul_error(self.root, self.output, ident)
        self.assertEqual(result['inherited_stages'], 3)
        for run, digest in digests.items():
            self.assertEqual(tree_fingerprint(self.output/'runs'/run), digest)
        self.assertFalse(json.loads((self.output/'runs'/ident/'result.json').read_text())['success'])

    def test_other_tool_error_is_not_reclassified(self):
        _, ident = self.interrupted()
        path = self.root/'runs'/ident/'events.jsonl'
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        for row in rows:
            if row.get('event') == 'tool_call' and row['call_id'] == 1:
                row['command'] = 'valid command'
        path.write_text('\n'.join(json.dumps(row) for row in rows)+'\n')
        with self.assertRaisesRegex(ValueError, 'not the diagnosed NUL'):
            reconcile.continue_nul_error(self.root, self.output, ident)
        self.regrader.assert_not_called()

    def test_changed_source_rejected_before_grading(self):
        _, ident = self.interrupted()
        (self.root/'runs'/ident/'source/main.py').write_text('modified')
        with self.assertRaisesRegex(ValueError, 'source changed'):
            reconcile.continue_nul_error(self.root, self.output, ident)
        self.regrader.assert_not_called()

    def test_changed_task_inputs_rejected_before_grading(self):
        _, ident = self.interrupted()
        self.fingerprints.return_value['original']['query-null/cases.json'] = 'changed'
        with self.assertRaisesRegex(ValueError, 'unrelated experiment inputs'):
            reconcile.continue_nul_error(self.root, self.output, ident)
        self.regrader.assert_not_called()

    def test_grading_failure_does_not_publish_runnable_plan_or_change_original(self):
        _, ident = self.interrupted()
        original = self.root/'runs'/ident
        digest = tree_fingerprint(original)
        self.regrader.side_effect = RuntimeError('Docker unavailable')
        with self.assertRaises(RuntimeError): reconcile.continue_nul_error(self.root, self.output, ident)
        self.assertFalse((self.output/'plan.json').exists())
        self.assertEqual(tree_fingerprint(original), digest)
        with self.assertRaisesRegex(ValueError, 'output must be new'):
            reconcile.continue_nul_error(self.root, self.output, ident)


if __name__ == '__main__': unittest.main()
