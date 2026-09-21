"""Publishing must not carry answer-containing records into Git."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from export_results import export, summarize
from report import render

COMMIT = 'a' * 40
CANARY = 'PRIVATE_REFERENCE_AND_MODEL_TEXT_MUST_NOT_BE_PUBLISHED'


def fixture(root):
    rows = [{'ordinal': i, 'task_id': f'UID{i:04}', 'status': 'completed', 'requests': 1,
             'elapsed_seconds': 1, 'answer_format_valid': True, 'answer': CANARY,
             'question': CANARY, 'records': '/private/operator/home', 'usage': {}}
            for i in range(1, 51)]
    metrics = {'arms': {arm: {'correct': 50, 'strict_accuracy': 1, 'one_percent_accuracy': 1}
                        for arm in ('baseline', 'adapted')},
               'results': [{'task_id': r['task_id'], 'baseline': 1, 'adapted': 1, 'answer': CANARY} for r in rows]}
    state = {'status': 'completed', 'started_utc': '2026-01-01', 'finished_utc': '2026-01-02',
             'model': 'accounts/fireworks/models/glm-5p3-flash', 'metrics': metrics,
             'arms': {arm: {'results': rows} for arm in ('baseline', 'adapted')},
             'learning': [{'block': i, 'activated': True, 'status': 'admitted', 'learner_status': 'completed',
                           'model_calls': 4, 'elapsed_seconds': 1, 'usage': {'completion_tokens': 100},
                           'public_feedback': CANARY, 'candidate': '/private/operator/memory'} for i in range(1, 11)]}
    (root / 'state.json').write_text(json.dumps(state))
    (root / 'freeze.json').write_text(json.dumps({'files': {}, 'framework_revision': COMMIT}))
    (root / 'private-evaluation').mkdir()
    for name in ('private-evaluation/paired-metrics.json', 'baseline-predictions.jsonl', 'adapted-predictions.jsonl', 'PROTOCOL.md'):
        (root / name).write_text(CANARY)
    return state


class PublicationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.root = self.base / 'run'
        self.root.mkdir()
        self.state = fixture(self.root)
        self.output = self.base / 'public'

    def write_state(self):
        (self.root / 'state.json').write_text(json.dumps(self.state))

    def test_export_is_allowlisted_and_cannot_publish_answers_or_local_paths(self):
        with contextlib.redirect_stdout(io.StringIO()):
            export(self.root, self.output, COMMIT)
        files = sorted(self.output.iterdir())
        self.assertEqual({p.name for p in files}, {'summary.json', 'per-task.json', 'provenance.json', 'report.html'})
        for p in files:
            self.assertNotIn(CANARY, p.read_text())
            self.assertNotIn('/private/operator', p.read_text())

    def test_incomplete_or_unpaired_campaign_is_not_published(self):
        self.state['arms']['adapted']['results'] = self.state['arms']['adapted']['results'][:-1]
        self.write_state()
        with self.assertRaises(ValueError):
            export(self.root, self.output, COMMIT)
        self.assertFalse(self.output.exists())

    def test_wrong_framework_commit_cannot_relabel_existing_scores(self):
        with self.assertRaisesRegex(ValueError, 'framework|Framework'):
            export(self.root, self.output, 'b' * 40)
        self.assertFalse(self.output.exists())

    def test_scores_must_agree_with_all_paired_records(self):
        self.state['metrics']['arms']['baseline']['correct'] = 49
        self.write_state()
        with self.assertRaisesRegex(ValueError, 'scores disagree'):
            export(self.root, self.output, COMMIT)

    def test_round_ten_is_never_labeled_as_used_and_partial_report_works(self):
        self.state['status'] = 'running'
        self.state.pop('metrics')
        self.write_state()
        summary, rows = summarize(self.root, COMMIT)
        self.assertFalse(summary['learning'][-1]['used_by_scored_future_questions'])
        self.assertEqual(summary['adapter_lineage'][-1]['adapter_round'], 9)
        html = render(summary, rows)
        self.assertIn('Pending', html)
        self.assertNotIn(CANARY, html)

    def test_html_escapes_provider_or_task_status_strings(self):
        self.state['status'] = '<script>alert(1)</script>'
        self.write_state()
        summary, rows = summarize(self.root, COMMIT)
        html = render(summary, rows)
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;', html)


if __name__ == '__main__':
    unittest.main()
