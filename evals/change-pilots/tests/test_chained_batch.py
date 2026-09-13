"""Exercise real chain planning, lineage export and records with fake native clients."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_native_batch
from experiments import chained_batch as chain
from experiments import native_batch as native
from experiments.results import ResultStore
from followups.run import runner


class ChainedBatchTests(unittest.TestCase):
    def setUp(self):
        test_native_batch.NativeBatchTests.setUp(self)
        self.stack.enter_context(patch.object(chain, 'fingerprints', return_value={'chain': 'frozen'}))
        self.stack.enter_context(patch.object(chain, 'load_starter', native.load_starter))
        original_export = native.export_public.side_effect
        def export(task, output, language):
            original_export(task, output, language)
            launcher = output/'starter'/'run.sh'
            launcher.write_text('#!/bin/sh\nexec python3 main.py\n')
            launcher.chmod(0o755)
            return output
        native.export_public.side_effect = export
        base = Path(__file__).resolve().parents[1]
        self.scores = {}
        for name, directory in [('public', base), ('heldout', base/'heldout')]:
            cases = runner.load_cases(directory, ['query-null'])
            self.scores[name] = {'passed': True, 'corpus_sha256': runner.corpus_digest(cases),
                                'cases': [{'id': c.id, 'status': 'pass'} for c in cases]}
        self.grader.return_value = self.scores
        self.seen_bundles = []
        original_agent = self.agent.side_effect
        def agent(*args, **kwargs):
            self.seen_bundles.append({p.relative_to(self.instances[-1].bundle).as_posix()
                                     for p in self.instances[-1].bundle.rglob('*')})
            return original_agent(*args, **kwargs)
        self.agent.side_effect = agent
        self.followup_grader = self.stack.enter_context(patch.object(chain, 'grade_followup',
            side_effect=lambda *a: {'scores': copy.deepcopy(self.scores)}))

    def plan(self):
        return chain.make_plan(self.root, ['luna'], ['query-null'], ['python'])

    def test_prism_briefing_is_preloaded_at_both_stages_only_for_prism(self):
        plan=chain.make_plan(self.root,['luna'],['query-null'],['prism','python','typescript'],prism_briefing=True)
        self.assertEqual(plan['language_context']['profile'],chain.BRIEFING_PROFILE)
        self.assertEqual(plan['language_context']['prism_briefing_sha256'],chain.file_hash(chain.PRISM_BRIEFING))
        for cell in plan['runs']:
            base=(native.native_prompt_for if cell['checkpoint']==1 else chain.stage_two_prompt)(cell['task'],cell['language'])
            if cell['language']=='prism':
                self.assertEqual(cell['prompt'],chain.PRISM_BRIEFING.read_text()+'\n\n---\n\n'+base)
            else:
                self.assertEqual(cell['prompt'],base)
                self.assertEqual(cell['language_context'],'baseline')
        chain.checked_plan(ResultStore(self.root))
        self.agent.assert_not_called()

    def test_baseline_keeps_original_prism_prompts(self):
        plan=chain.make_plan(self.root,['luna'],['query-null'],['prism'])
        self.assertEqual(plan['language_context'],{'profile':'baseline','prism_briefing_sha256':None})
        for cell in plan['runs']:
            base=(native.native_prompt_for if cell['checkpoint']==1 else chain.stage_two_prompt)(cell['task'],cell['language'])
            self.assertEqual(cell['prompt'],base)

    def test_briefing_plan_rejects_missing_context_or_changed_digest(self):
        plan=chain.make_plan(self.root,['luna'],['query-null'],['prism'],prism_briefing=True)
        path=self.root/'plan.json';path.chmod(0o600)
        digest=plan['language_context']['prism_briefing_sha256']
        plan['language_context']['prism_briefing_sha256']='tampered'
        path.write_text(json.dumps(plan))
        with self.assertRaisesRegex(ValueError,'language briefing changed'):
            chain.checked_plan(ResultStore(self.root))
        plan['language_context']['prism_briefing_sha256']=digest
        plan['runs'][1]['prompt']='Missing context'
        path.write_text(json.dumps(plan))
        with self.assertRaisesRegex(ValueError,'missing its frozen briefing'):
            chain.checked_plan(ResultStore(self.root))
        self.agent.assert_not_called()

    def test_pair_carries_only_frozen_source_in_fresh_session_and_resumes(self):
        plan = self.plan(); first, second = plan['runs']
        self.assertEqual(first['checkpoint'], 1)
        self.assertEqual(second['parent_run_id'], first['run_id'])
        self.assertNotIn('checkpoint two', first['prompt'])
        self.assertNotIn('starter_sha256', second)
        self.assertEqual(chain.execute_plan(self.root, 1)['runs_finished'], 1)
        store = ResultStore(self.root); parent = store.run_dir(first['run_id'])
        original = (parent/'result.json').read_bytes()
        self.assertEqual(chain.execute_plan(self.root)['runs_finished'], 1)
        child = store.run_dir(second['run_id'])
        self.assertEqual((child/'baseline/main.py').read_bytes(), (parent/'source/main.py').read_bytes())
        self.assertNotEqual((child/'baseline/main.py').read_bytes(), (parent/'baseline/main.py').read_bytes())
        self.assertEqual((parent/'result.json').read_bytes(), original)
        self.assertEqual(self.clients.call_count, 2)
        self.assertEqual(self.exports, [('query-null', 'python')])
        self.assertEqual(self.agent.call_args_list[0].args[2], first['prompt'])
        self.assertEqual(self.agent.call_args_list[1].args[2], second['prompt'])
        receipt = json.loads((child/'lineage.json').read_text())
        self.assertEqual(receipt['parent_run_id'], first['run_id'])
        self.assertTrue(receipt['parent_passed'])
        self.assertTrue(self.seen_bundles[1])
        for filename in self.seen_bundles[1]:
            self.assertNotIn(Path(filename).name, ('result.json', 'lineage.json', 'metadata.json'))
        self.assertEqual(chain.execute_plan(self.root)['runs_finished'], 0)
        self.assertEqual(chain.summary(store)['both_checkpoints_passed'], 1)
        self.assertEqual({g['checkpoint'] for g in store.summary()['groups']}, {1, 2})
        blind = store.get_run(second['run_id'])
        self.assertEqual(blind['checkpoint'], 2)
        self.assertNotIn('parent_run_id', blind)
        self.assertNotIn('scores', blind)
        self.assertTrue(store.review_context(second['run_id'])['has_previous_spec'])
        self.assertIn('NULL', store.review_context(second['run_id'], 'previous_spec')['text'])
        with self.assertRaises(ValueError): store.review_context(second['run_id'], 'lineage.json')

    def test_incorrect_parent_continues_without_repair_or_grade_feedback(self):
        plan = self.plan()
        self.scores['heldout']['passed'] = False
        self.scores['heldout']['cases'][0]['status'] = 'fail'
        chain.execute_plan(self.root, 1)
        self.scores['heldout']['passed'] = True
        self.scores['heldout']['cases'][0]['status'] = 'pass'
        result = chain.execute_plan(self.root)['summary']
        self.assertEqual(result['checkpoint_one_passed'], 0)
        self.assertEqual(result['checkpoint_two_passed'], 1)
        self.assertEqual(result['checkpoint_two_with_passing_parent'], 0)
        self.assertEqual(result['both_checkpoints_passed'], 0)
        child = ResultStore(self.root).run_dir(plan['runs'][1]['run_id'])
        self.assertFalse(json.loads((child/'lineage.json').read_text())['parent_passed'])
        self.assertNotIn('heldout', self.agent.call_args.args[2])

    def test_nonexecutable_launcher_carries_forward_with_its_original_mode(self):
        plan = self.plan()
        snapshot = native.DockerSandbox.snapshot
        def broken_launcher(sandbox, destination):
            result = snapshot(sandbox, destination)
            (result/'run.sh').chmod(0o644)
            return result
        with patch.object(native.DockerSandbox, 'snapshot', broken_launcher):
            result = chain.execute_plan(self.root)
        self.assertIsNone(result['stopped_on_infrastructure_run'])
        child = ResultStore(self.root).run_dir(plan['runs'][1]['run_id'])
        receipt = json.loads((child/'lineage.json').read_text())
        self.assertEqual(receipt['parent_run_id'], plan['runs'][0]['run_id'])
        self.assertEqual(self.clients.call_count, 2)

    def test_invalid_source_creates_explicit_unattempted_child(self):
        self.plan()
        with patch.object(native.DockerSandbox, 'snapshot', side_effect=native.InvalidSubmissionError('bad source')):
            result = chain.execute_plan(self.root)['summary']
        self.assertEqual(result['finished_stages'], 2)
        self.assertEqual(result['not_attempted'], 1)
        self.assertEqual(self.agent.call_count, 1)
        self.followup_grader.assert_not_called()

    def test_infrastructure_stops_before_child_and_is_never_retried(self):
        self.plan(); self.agent_result['stop_reason'] = 'native_client_error'
        self.assertTrue(chain.execute_plan(self.root)['stopped_on_infrastructure_run'])
        with self.assertRaisesRegex(ValueError, 'infrastructure'): chain.execute_plan(self.root)
        self.assertEqual(self.agent.call_count, 1)

    def test_changed_parent_rejected_before_second_model_call(self):
        plan = self.plan(); chain.execute_plan(self.root, 1)
        source = ResultStore(self.root).run_dir(plan['runs'][0]['run_id'])/'source/main.py'
        source.write_text('modified after grading')
        with self.assertRaisesRegex(ValueError, 'parent source changed'): chain.execute_plan(self.root)
        self.assertEqual(self.agent.call_count, 1)

    def test_wrong_parent_metadata_rejected_before_any_model_call(self):
        plan = self.plan(); plan['runs'][1]['language'] = 'typescript'
        path = self.root/'plan.json'; path.chmod(0o600); path.write_text(json.dumps(plan))
        with self.assertRaisesRegex(ValueError, 'parent relationship'): chain.execute_plan(self.root)
        self.agent.assert_not_called()

    def test_stop_marker_and_interrupted_run_start_no_session(self):
        plan = self.plan(); marker = self.root/'STOP_AFTER_CURRENT'; marker.touch()
        self.assertEqual(chain.execute_plan(self.root)['runs_finished'], 0)
        marker.unlink(); cell = plan['runs'][0]
        ResultStore(self.root).create_run(cell['run_id'], cell)
        with self.assertRaisesRegex(ValueError, 'interrupted'): chain.execute_plan(self.root)
        self.agent.assert_not_called()

    def test_default_matrix_preserves_model_language_and_budget_for_every_pair(self):
        plan = chain.make_plan(self.root)
        self.assertEqual(len(plan['runs']), 96)
        for first, second in zip(plan['runs'][::2], plan['runs'][1::2]):
            for key in ('model', 'language', 'task', 'effort', 'repetition', 'chain_id'):
                self.assertEqual(first[key], second[key])
            self.assertEqual(second['parent_run_id'], first['run_id'])
        self.assertEqual(plan['budgets']['wall_timeout_seconds'], 1800)
        self.assertFalse(plan['accounting']['api_fallback'])


if __name__ == '__main__': unittest.main()
