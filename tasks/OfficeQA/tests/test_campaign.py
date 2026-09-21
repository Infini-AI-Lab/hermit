from contextlib import ExitStack, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import campaign

class EvidenceTests(unittest.TestCase):
    def fixture(self,root):
        tasks=[];results=[];keys=[]
        for i in range(1,7):
            task={'task_id':str(i),'question':'Synthetic question '+str(i)}
            tasks.append(task);keys.append({'task_id':str(i),'answer':f'OFFICIAL_{i}'})
            if i>5:continue
            base=root/'runs/adapted'/f'{i:03}';(base/'records').mkdir(parents=True)
            for name,value in [('task.json',{'ordinal':i,**task}),('prediction.json',{'task_id':str(i),'answer':'Model prediction'}),('summary.json',{'status':'completed'})]:
                (base/name).write_text(json.dumps(value))
            events=[{'type':'item.completed','item':{'type':'reasoning','text':'PRIVATE_REASONING'}},{'type':'item.completed','item':{'type':'command_execution','aggregated_output':'OBSERVED'}},{'type':'item.completed','item':{'type':'agent_message','text':'PUBLIC_MODEL'}}]
            (base/'records/events.jsonl').write_text('\n'.join(json.dumps(x) for x in events))
            results.append({'ordinal':i,'task_id':str(i),'prediction_sha256':campaign.sha(base/'prediction.json')})
        (root/'prepared/private').mkdir(parents=True)
        (root/'prepared/private/answers.jsonl').write_text(''.join(json.dumps(x)+'\n' for x in keys))
        return tasks,results

    def test_completed_references_only_after_seal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve();tasks,results=self.fixture(root)
            (root/'source_hints.json').write_text('SOURCE_HINTS_EXCLUDED')
            (root/'runs/baseline').mkdir();(root/'runs/baseline/review.json').write_text('BASELINE_EXCLUDED')
            with patch.object(campaign,'ROOT',root),patch.object(campaign.data,'load_tasks',return_value=tasks):
                out=campaign.assemble_evidence(1,results);campaign.verify_learning_seals()
                with self.assertRaises(FileExistsError):campaign.assemble_evidence(1,results)
            text='\n'.join(p.read_text() for p in out.rglob('*') if p.is_file())
            for forbidden in ('OFFICIAL_6','SOURCE_HINTS_EXCLUDED','BASELINE_EXCLUDED','PRIVATE_REASONING'):self.assertNotIn(forbidden,text)
            for required in ('OBSERVED','PUBLIC_MODEL','OFFICIAL_1','OFFICIAL_5'):self.assertIn(required,text)
            manifest=json.loads((out/'manifest.json').read_text())
            self.assertTrue(manifest['includes_gold']);self.assertFalse(manifest['includes_future_questions'])
            self.assertEqual([x['ordinal'] for x in manifest['episodes']],list(range(1,6)))
            seal=root/'private-learning-seals/01.json';receipt=json.loads(seal.read_text())
            self.assertEqual(manifest['batch_seal']['sha256'],campaign.sha(seal))
            self.assertLessEqual(receipt['sealed_utc'],receipt['reference_released_utc'])
            for ep in receipt['episodes']:self.assertEqual(ep['reference_answer_sha256'],campaign.sha(out/f"{ep['ordinal']:03}"/'reference_answer.json'))
            (root/'runs/adapted/001/prediction.json').write_text('{}')
            with patch.object(campaign,'ROOT',root):
                with self.assertRaisesRegex(ValueError,'changed after'):campaign.verify_learning_seals()

    def test_mutated_submission_prevents_reference_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve();tasks,results=self.fixture(root)
            (root/'runs/adapted/001/prediction.json').write_text(json.dumps({'task_id':'1','answer':'changed'}))
            with patch.object(campaign,'ROOT',root),patch.object(campaign.data,'load_tasks',return_value=tasks):
                with self.assertRaisesRegex(ValueError,'changed before'):campaign.assemble_evidence(1,results)
            self.assertFalse((root/'evidence').exists())

    def test_wrong_batch_prevents_reference_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve();tasks,results=self.fixture(root)
            with patch.object(campaign,'ROOT',root),patch.object(campaign.data,'load_tasks',return_value=tasks):
                with self.assertRaisesRegex(ValueError,'exact current'):campaign.assemble_evidence(2,results)
            self.assertFalse((root/'evidence').exists())

    def test_nonfive_batch_rejected(self):
        with self.assertRaises(ValueError):campaign.assemble_evidence(1,[])

    def test_task_identity_comes_from_trusted_selection_not_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve();tasks,results=self.fixture(root)
            results[0]['task_id']='6'
            with patch.object(campaign,'ROOT',root),patch.object(campaign.data,'load_tasks',return_value=tasks):
                with self.assertRaisesRegex(ValueError,'identity mismatch'):
                    campaign.assemble_evidence(1,results)
            self.assertFalse((root/'private-learning-seals').exists())
            self.assertFalse((root/'evidence').exists())

    def test_reference_failure_keeps_partial_seal_and_cannot_overwrite_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve();tasks,results=self.fixture(root)
            answer_path=root/'prepared/private/answers.jsonl'
            original=answer_path.read_bytes()
            answer_path.write_text('')
            with patch.object(campaign,'ROOT',root),patch.object(campaign.data,'load_tasks',return_value=tasks):
                with self.assertRaisesRegex(ValueError,'Missing completed-batch reference'):
                    campaign.assemble_evidence(1,results)
                seal_path=root/'private-learning-seals/01.json'
                sealed_bytes=seal_path.read_bytes()
                receipt=json.loads(sealed_bytes)
                self.assertTrue(receipt['predictions_sealed'])
                self.assertNotIn('reference_released_utc',receipt)
                self.assertFalse((root/'evidence').exists())
                answer_path.write_bytes(original)
                with self.assertRaises(FileExistsError):campaign.assemble_evidence(1,results)
                self.assertEqual(seal_path.read_bytes(),sealed_bytes)
                campaign.verify_learning_seals()


class CancellationTests(unittest.TestCase):
    def test_actor_sealing_or_learner_failure_stops_future_dispatch_in_both_arms(self):
        import learning
        import report
        for failure_stage in ('actor','seal','learner'):
            with self.subTest(stage=failure_stage), tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary).resolve()
                tasks=[{'task_id':str(i),'question':'Synthetic question'} for i in range(1,11)]
                stopped=threading.Event()
                baseline_started=threading.Event()
                seen={'baseline':[],'adapted':[]}
                seen_lock=threading.Lock()
                def actor(_key,arm,ordinal,task,_adapter):
                    with seen_lock:seen[arm].append(ordinal)
                    if arm=='baseline':
                        baseline_started.set()
                        if not stopped.wait(3):raise AssertionError('Other-arm failure did not signal cancellation')
                    else:
                        if not baseline_started.wait(3):raise AssertionError('Baseline did not enter its first actor')
                        if failure_stage=='actor':raise RuntimeError('synthetic actor failure')
                    return {'ordinal':ordinal,'task_id':task['task_id'],'status':'completed',
                            'answer':'synthetic answer','requests':1,'transport_failed':False}
                with ExitStack() as stack:
                    # Replace only campaign's namespace, never threading.Event globally;
                    # the real thread pool and its synchronization remain intact.
                    stack.enter_context(patch.object(campaign,'threading',SimpleNamespace(
                        RLock=threading.RLock,Event=lambda:stopped)))
                    stack.enter_context(patch.object(campaign,'ROOT',root))
                    stack.enter_context(patch.object(campaign.data,'load_tasks',return_value=tasks))
                    stack.enter_context(patch.object(campaign.data,'verify_prepared',return_value={'verified':True}))
                    stack.enter_context(patch.object(campaign,'freeze',return_value={}))
                    stack.enter_context(patch.object(campaign,'verify_freeze'))
                    stack.enter_context(patch.object(campaign,'verify_learning_seals'))
                    stack.enter_context(patch.object(campaign,'actor',side_effect=actor))
                    assemble=stack.enter_context(patch.object(campaign,'assemble_evidence',
                        side_effect=RuntimeError('synthetic seal failure') if failure_stage=='seal' else None,
                        return_value=root/'synthetic-evidence'))
                    learner=stack.enter_context(patch.object(learning,'run_learning',
                        side_effect=RuntimeError('synthetic learner failure')))
                    grade=stack.enter_context(patch.object(campaign.data,'grade'))
                    seal=stack.enter_context(patch.object(campaign.data,'seal_predictions'))
                    stack.enter_context(patch.object(report,'build'))
                    stack.enter_context(redirect_stdout(io.StringIO()))
                    with self.assertRaisesRegex(RuntimeError,'synthetic '+failure_stage+' failure'):
                        campaign.run('fixture-secret')
                    grade.assert_not_called();seal.assert_not_called()
                    self.assertEqual(assemble.call_count,0 if failure_stage=='actor' else 1)
                    self.assertEqual(learner.call_count,1 if failure_stage=='learner' else 0)
                self.assertTrue(stopped.is_set())
                self.assertEqual(seen['baseline'],[1])
                self.assertEqual(seen['adapted'],[1] if failure_stage=='actor' else [1,2,3,4,5])
                state=json.loads((root/'state.json').read_text())
                self.assertEqual(state['status'],'stopped_on_error')
                self.assertEqual(state['arms']['baseline']['completed'],1)
                self.assertFalse((root/'baseline-predictions.jsonl').exists())
                self.assertFalse((root/'adapted-predictions.jsonl').exists())

if __name__=='__main__':unittest.main()
