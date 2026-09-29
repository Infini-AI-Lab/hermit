"""Offline checks of the controller post-mortem: grades, phases, checkers, lesson replay."""
import json
from pathlib import Path
import tempfile
import unittest

import officeqa_prepare
import officeqa_tools
import postmortem as P
from test_learning import setup_session

REWARD = officeqa_prepare.VENDOR / 'reward.py'
REFERENCE = 'Synthetic reference response.'


def fixture_corpus(base):
    corpus = base / 'corpus'; corpus.mkdir(parents=True)
    (corpus / 'fixture.txt').write_text('Table 1. Receipts\nTotal receipts ... 1,234.5\nOutlays ... (56)\n\nEnd\n')
    officeqa_tools.build_index(corpus, base / 'corpus.sqlite', expected_documents=1)
    return corpus, base / 'corpus.sqlite'


def lesson(status, check, brief='Count the cells.'):
    return (f'last_confirmed_t: 1\nscope: table:receipts\nderived_from: postmortem\nbrief: {brief}\n'
            f'status: {status}\nstep: S4\ncheck: {json.dumps(check)}\nverified_by: postmortem:001\n\nBody.\n')


class Grading(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.session = setup_session(self.base / 'session')
        self.corpus, self.index = fixture_corpus(self.base)

    def test_grade_block_tolerates_fixture_without_summary_or_failure_files(self):
        verdicts = P.grade_block(self.session / 'evidence', self.base / 'pm', 1, REWARD)
        self.assertEqual(verdicts['grader_sha256'], officeqa_prepare.REWARD_SHA256)
        self.assertEqual([e['ordinal'] for e in verdicts['episodes']], [1, 2, 3, 4, 5])
        first = verdicts['episodes'][0]
        self.assertEqual((first['strict'], first['one_percent'], first['status'], first['wm_calls']), (0, 0, None, None))
        self.assertEqual(json.loads((self.base / 'pm/verdicts.json').read_text()), verdicts)
        self.assertEqual(json.loads((self.base / 'pm/001.json').read_text()), first)
        self.assertEqual(len(P.phases(verdicts)), 5)
        self.assertIn('episode 003', P.phases(verdicts)[2])
        text = P.panel(verdicts, {'verified': 2, 'demoted': 1, 'lessons': [{'id': 'L-x', 'status_before': 'verified', 'passed': False}]})
        self.assertIn('strict 0/5', text); self.assertIn('Demoted: L-x', text)
        self.assertIn('Synthetic placeholder question.', text)

    def test_correct_submission_yields_no_phase(self):
        for n in range(1, 6):
            path = self.session / f'evidence/{n:03}/prediction.json'
            path.write_text(json.dumps({'task_id': f'fixture-{n-1}', 'answer': REFERENCE}))
        (self.session / 'evidence/003/failure.json').write_text(json.dumps({'status': 'failed', 'terminal_event': 'turn.failed', 'error_text': 'stream lost'}))
        verdicts = P.grade_block(self.session / 'evidence', self.base / 'pm', 1, REWARD)
        self.assertEqual([e['strict'] for e in verdicts['episodes']], [1] * 5)
        self.assertEqual(verdicts['episodes'][2]['error_text'], 'stream lost')
        self.assertEqual(P.phases(verdicts), [])
        self.assertIn('turn.failed (stream lost)', P.panel(verdicts))

    def test_goal_text_and_prompt_vocabulary(self):
        for architecture in ('mwm', 'prompt_only'):
            text = P.goal_text(architecture)
            for word in ('verify_cell', 'verify_answer', 'grep -n', 'S7', 'status: verified|candidate', 'stop with no tool call'):
                self.assertIn(word, text, (architecture, word))
        self.assertIn('memory/L-<slug>.md', P.goal_text('mwm'))
        self.assertIn('### L-<slug>', P.goal_text('prompt_only'))
        self.assertEqual([d['function']['name'] for d in P.DECLARATIONS], ['verify_answer', 'verify_cell'])

    def context(self):
        return {'evidence': self.session / 'evidence', 'manifest': json.loads((self.session / 'evidence/manifest.json').read_text()),
                'corpus': self.corpus, 'index': self.index, 'reward': REWARD}

    def test_verify_answer_grades_released_ordinals_only(self):
        text, ok = P.call('verify_answer', {'ordinal': 1, 'candidate': REFERENCE}, self.context())
        self.assertTrue(ok); value = json.loads(text)
        self.assertEqual((value['strict'], value['one_percent'], value['normalized_reference']), (1, 1, REFERENCE))
        self.assertNotIn('rationale', text)
        text, ok = P.call('verify_answer', {'ordinal': 1, 'candidate': 'Synthetic response.'}, self.context())
        self.assertTrue(ok); self.assertEqual(json.loads(text)['strict'], 0)
        text, ok = P.call('verify_answer', {'ordinal': 9, 'candidate': REFERENCE}, self.context())
        self.assertFalse(ok); self.assertIn('not in this released block', text)

    def test_verify_cell_reads_one_line_with_neighbours(self):
        text, ok = P.call('verify_cell', {'file': 'fixture.txt', 'line': 2, 'contains': '1234.5'}, self.context())
        self.assertTrue(ok); value = json.loads(text)
        self.assertTrue(value['found']); self.assertEqual(value['line_text'], 'Total receipts ... 1,234.5')
        self.assertEqual(value['neighbours'], ['Table 1. Receipts', 'Total receipts ... 1,234.5', 'Outlays ... (56)', ''])
        text, ok = P.call('verify_cell', {'file': 'fixture.txt', 'line': 2, 'contains': '999'}, self.context())
        self.assertTrue(ok); self.assertFalse(json.loads(text)['found'])
        for args in ({'file': 'missing.txt', 'line': 1, 'contains': 'x'}, {'file': 'fixture.txt', 'line': 40, 'contains': 'x'},
                     {'file': 'fixture.txt', 'line': 1, 'contains': ''}):
            _, ok = P.call('verify_cell', args, self.context())
            self.assertFalse(ok, args)


class Lessons(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.session = setup_session(self.base / 'session')
        self.corpus, self.index = fixture_corpus(self.base)
        self.candidate = self.session / 'adapter'
        self.evidence = {n: self.session / f'evidence/{n:03}' for n in range(1, 6)}

    def test_mwm_replay_promotes_demotes_and_rewrites_status_lines(self):
        memory = self.candidate / 'memory'; memory.mkdir()
        (memory / 'L-good.md').write_text(lesson('candidate', [{'kind': 'cell', 'file': 'fixture.txt', 'line': 2, 'contains': '1,234.5'},
                                                            {'kind': 'answer', 'ordinal': 1, 'candidate': REFERENCE}]))
        (memory / 'L-future.md').write_text(lesson('verified', [{'kind': 'answer', 'ordinal': 9, 'candidate': REFERENCE}]))
        (memory / 'L-empty.md').write_text(lesson('verified', []))
        (memory / 'L-python.md').write_text(lesson('candidate', [{'kind': 'python', 'code': 'print(1)'}]))
        (memory / 'plain-entry.md').write_text('last_confirmed_t: 1\nscope: x\nderived_from: y\nbrief: not a lesson\n')
        (self.session / 'scratch/postmortem').mkdir(); (self.session / 'scratch/postmortem/001.json').write_text('{}')
        summary = P.replay(self.candidate, 'mwm', 1, self.evidence, self.corpus, self.index, REWARD)
        by_id = {l['id']: l for l in summary['lessons']}
        self.assertEqual(set(by_id), {'L-good', 'L-future', 'L-empty', 'L-python'})
        self.assertEqual((by_id['L-good']['status_before'], by_id['L-good']['status_after']), ('candidate', 'verified'))
        self.assertEqual(by_id['L-future']['status_after'], 'candidate')
        self.assertIn('not released', by_id['L-future']['checks'][0]['detail'])
        self.assertFalse(by_id['L-empty']['passed']); self.assertFalse(by_id['L-python']['passed'])
        self.assertEqual({k: summary[k] for k in ('verified', 'candidate', 'demoted', 'promoted', 'wrong', 'records')},
                         {'verified': 1, 'candidate': 3, 'demoted': 2, 'promoted': 1, 'wrong': 2, 'records': 1})
        self.assertIn('status: verified\n', (memory / 'L-good.md').read_text())
        self.assertIn('status: candidate\n', (memory / 'L-future.md').read_text())
        self.assertEqual(json.loads((self.session / 'control/postmortem-replay.json').read_text()), summary)
        self.assertEqual(P.served_bytes(self.candidate, 'mwm'), {'brief': 0, 'lessons': 4, 'verified_lessons': 1})

    def test_prompt_only_lessons_live_in_adapter_md_sections(self):
        text = ('# Adapter context\n\n## Procedure\n- round last\n\n## Conventions\n\n## Lessons\n\n### L-receipts\n'
                'status: candidate\nscope: table:receipts\nstep: S4\ncheck: [{"kind":"cell","file":"fixture.txt","line":2,"contains":"1,234.5"}]\n'
                'verified_by: postmortem:001\nbrief: Receipts table cell.\nBody line.\n\n### L-broken\nstatus: verified\nscope: harness\nstep: S7\n'
                'check: not json\nbrief: Broken.\n\n## Open questions\n')
        (self.candidate / 'ADAPTER.md').write_text(text)
        lessons = P.parse_lessons(self.candidate, 'prompt_only')
        self.assertEqual([(l['id'], l['status'], l['step'], len(l['check'])) for l in lessons],
                         [('L-receipts', 'candidate', 'S4', 1), ('L-broken', 'verified', 'S7', 0)])
        summary = P.replay(self.candidate, 'prompt_only', 1, self.evidence, self.corpus, self.index, REWARD)
        self.assertEqual((summary['verified'], summary['demoted'], summary['promoted']), (1, 1, 1))
        after = (self.candidate / 'ADAPTER.md').read_text()
        self.assertIn('### L-receipts\nstatus: verified\n', after)
        self.assertIn('### L-broken\nstatus: candidate\n', after)
        self.assertTrue(after.endswith('## Open questions\n'))
        self.assertEqual(P.served_bytes(self.candidate, 'prompt_only')['adapter_md'], len(after))

    def test_missing_status_line_is_inserted(self):
        memory = self.candidate / 'memory'; memory.mkdir()
        (memory / 'L-nostatus.md').write_text('last_confirmed_t: 1\nscope: harness\nderived_from: x\nbrief: b\nstep: S7\ncheck: []\n')
        [entry] = P.parse_lessons(self.candidate, 'mwm')
        self.assertIsNone(entry['status'])
        P.write_status(self.candidate, 'mwm', entry, 'candidate')
        self.assertTrue((memory / 'L-nostatus.md').read_text().startswith('status: candidate\nlast_confirmed_t: 1\n'))

    def test_prompt_only_replay_keeps_structure_with_missing_and_duplicate_status_lines(self):
        cell = json.dumps([{'kind': 'cell', 'file': 'fixture.txt', 'line': 2, 'contains': '1,234.5'}])
        text = ('# Adapter context\n\n## Lessons\n\n### L-a\nstep: S4\ncheck: []\n### L-b\nstep: S4\ncheck: []\n'
                f'### L-c\nstatus: candidate\nstep: S4\ncheck: {cell}\nbrief: c\nstatus: verified\nBody.\n## Open questions\n')
        (self.candidate / 'ADAPTER.md').write_text(text)
        summary = P.replay(self.candidate, 'prompt_only', 1, self.evidence, self.corpus, self.index, REWARD)
        after = (self.candidate / 'ADAPTER.md').read_text()
        self.assertIn('### L-a\nstatus: candidate\nstep: S4\n', after)
        self.assertIn('### L-b\nstatus: candidate\nstep: S4\n', after)
        self.assertIn('### L-c\nstatus: verified\nstep: S4\n', after)
        self.assertEqual(after.count('status:'), 3)
        self.assertEqual([(l['id'], l['status']) for l in P.parse_lessons(self.candidate, 'prompt_only')],
                         [('L-a', 'candidate'), ('L-b', 'candidate'), ('L-c', 'verified')])
        self.assertEqual((summary['verified'], summary['candidate']), (1, 2))

    def test_body_status_line_never_counts_and_ordinal_strings_are_accepted(self):
        memory = self.candidate / 'memory'; memory.mkdir()
        (memory / 'L-sneaky.md').write_text(lesson('candidate', []) + 'status: verified\n')
        (memory / 'L-text.md').write_text(lesson('candidate', [{'kind': 'answer', 'ordinal': '1', 'candidate': REFERENCE}]))
        summary = P.replay(self.candidate, 'mwm', 1, self.evidence, self.corpus, self.index, REWARD)
        by_id = {l['id']: l for l in summary['lessons']}
        self.assertFalse(by_id['L-sneaky']['passed']); self.assertTrue(by_id['L-text']['passed'])
        sneaky = (memory / 'L-sneaky.md').read_text()
        self.assertEqual(sneaky.count('status:'), 1); self.assertIn('status: candidate\n', sneaky)

    def test_consult_rule_matches_the_served_copy(self):
        import importlib
        wm_iface = importlib.import_module('harnessadapter.architectures.mwm.mwm_core.wm_iface')
        self.assertEqual(P.CONSULT_RULE, wm_iface.CONSULT_RULE)


if __name__ == '__main__':
    unittest.main(verbosity=2)
