import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import campaign


class EvidenceTests(unittest.TestCase):
    def test_only_current_adapted_public_records_are_copied(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            results=[]
            for i in range(1,6):
                base=root/'runs/adapted'/f'{i:03}'
                (base/'records').mkdir(parents=True)
                for name,value in [('task.json',{'task_id':str(i),'question':'Synthetic question'}),
                                   ('prediction.json',{'task_id':str(i),'answer':'Model prediction'}),
                                   ('summary.json',{'status':'completed'})]:
                    (base/name).write_text(json.dumps(value))
                events=[{'type':'item.completed','item':{'type':'reasoning','text':'PRIVATE_REASONING'}},
                        {'type':'item.completed','item':{'type':'command_execution','aggregated_output':'OBSERVED'}},
                        {'type':'item.completed','item':{'type':'agent_message','text':'PUBLIC_MODEL'}}]
                (base/'records/events.jsonl').write_text('\n'.join(json.dumps(x) for x in events))
                results.append({'ordinal':i,'task_id':str(i)})
            (root/'gold.json').write_text('SECRET_GOLD')
            (root/'runs/baseline').mkdir()
            (root/'runs/baseline/review.json').write_text('BASELINE_EXCLUDED')
            with patch.object(campaign,'ROOT',root):
                out=campaign.assemble_evidence(1,results)
                with self.assertRaises(FileExistsError): campaign.assemble_evidence(1,results)
            text='\n'.join(p.read_text() for p in out.rglob('*') if p.is_file())
            for forbidden in ('SECRET_GOLD','BASELINE_EXCLUDED','PRIVATE_REASONING'):
                self.assertNotIn(forbidden,text)
            self.assertIn('OBSERVED',text)
            self.assertIn('PUBLIC_MODEL',text)
            manifest=json.loads((out/'manifest.json').read_text())
            self.assertEqual(manifest['source_arm'],'adapted')
            self.assertEqual([x['ordinal'] for x in manifest['episodes']],list(range(1,6)))
    def test_nonfive_batch_rejected(self):
        with self.assertRaises(ValueError): campaign.assemble_evidence(1,[])


if __name__=='__main__': unittest.main()
