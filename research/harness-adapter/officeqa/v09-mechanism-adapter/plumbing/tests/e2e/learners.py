"""Scripted learner models for the toy campaign (tests/fake_learner_model.ScriptedModel; $0, no network).

Both read the released listing first (`list_episodes`) and base every proposal on what it shows (wrong
episodes = the pool), so their proposals are a deterministic function of the evidence:
- text learner: one scoped note for the first failure class it recognises among the wrong episodes, with
  support = the wrong episodes whose question contains the note's trigger;
- mechanism learner: one new rule (a stricter copy of the printing rule, targeted at the wrong episodes;
  with 5 released episodes it cannot pass the precision minimum of 10 right episodes, so it is rejected), one
  policy knob (loop_threshold 3 -> 4, admitted on its range with a replay record) and the same scoped note.
The note texts carry the phrases the weak actor reacts to (tests/e2e/weak_actor.py), so paired re-run
verification through the campaign's actor runner flips the support questions.
"""
from __future__ import annotations

import json
import re

from fake_learner_model import ScriptedModel, calls, say, tool_call, tool_results

# failure class -> (note id, trigger, text); the first class whose trigger a wrong question contains is used
NOTE_CLASSES = [
    ('revised_figures', 'widgets shipped',
     'Several widget figures of 2020 were revised after their first printing. Read the revised printing of the '
     '2020 bulletin (the file ending in _b) unless the question asks for the first printing.'),
    ('decimal_rates', 'as a decimal',
     'A growth rate asked for as a decimal is the ratio itself: answer as a decimal, not multiplied by 100.'),
    ('two_values', 'both', 'When a question asks for both values, give both values, comma-separated, in the order asked.'),
]
LINE = re.compile(r'^q(?P<o>\d{3}) b(?P<b>\d+) strict (?P<s>\d) 1% \d (?P<status>\S+)(?P<lost> transport-lost)? \| Q: (?P<q>.*?) \|')


def listing(messages) -> list[dict]:
    """Episodes of the last list_episodes result: ordinal, strict, question (as the learner sees them)."""
    for result in reversed(tool_results(messages)):
        if result.get('part') == 'list':
            rows = []
            for line in result['content'].splitlines():
                match = LINE.match(line)
                if match:
                    rows.append({'ordinal': int(match['o']), 'strict': int(match['s']), 'lost': bool(match['lost']),
                                 'question': match['q'].lower()})
            return rows
    return []


def note_proposal(rows) -> dict | None:
    wrong = [r for r in rows if not r['strict'] and not r['lost']]
    for note_id, trigger, text in NOTE_CLASSES:
        support = [r['ordinal'] for r in wrong if trigger in r['question']][:5]
        if support:
            return {'id': note_id, 'triggers': [trigger], 'text': text, 'support': support,
                    'rationale': f'the wrong episodes {support} share this failure; the note names the fix'}
    return None


def text_model() -> ScriptedModel:
    def propose(messages):
        proposal = note_proposal(listing(messages))
        if proposal is None:
            return say('No wrong released episode matches a failure I can address; no proposal.')
        return calls(tool_call('propose_note', **proposal))
    return ScriptedModel([calls(tool_call('list_episodes')), propose,
                          say('Report: one scoped note proposed for the wrong episodes of this block.')])


RULE = {'id': 'revised_cells', 'when': [{'pred': 'later_printing', 'args': {}}],
        'finding': 'calc {compute.sha} reads {cell.locator}; the revised printing {later.locator} shows {later.value}.',
        'offer': {'op': 'recompute', 'args': {}}, 'priority': 20, 'triggers': []}


def mechanism_model(*, note: bool = True, edit: str = 'policy') -> ScriptedModel:
    """edit: 'policy' = set_policy loop_threshold 4; 'priority' = modify_rule answer_count priority 10 -> 12
    (a priority-only modification, admitted after the schema check)."""
    def edits(messages):
        rows = listing(messages)
        targets = [r['ordinal'] for r in rows if not r['strict'] and not r['lost']][:3]
        batch = [tool_call('set_policy', key='loop_threshold', value=4,
                           rationale='a repeated lookup with identical output was noted only after 3 runs')
                 if edit == 'policy' else
                 tool_call('modify_rule', id='answer_count', patch={'priority': 12},
                           rationale='the printing check should be shown before the count check')]
        if targets:
            batch.insert(0, tool_call('propose_rule', rule=RULE, targets=targets,
                                      rationale='the wrong episodes used a first-printing cell'))
        return calls(*batch)

    def propose_note_turn(messages):
        # the listing is two tool rounds back; re-read it from the whole conversation
        rows = []
        for message in messages:
            if message.get('role') == 'tool':
                value = json.loads(message['content'])
                if value.get('part') == 'list':
                    rows = listing([{'role': 'assistant'}, message])
        proposal = note_proposal(rows)
        return calls(tool_call('propose_note', **proposal)) if proposal else say('No note.')
    turns = [calls(tool_call('list_episodes')), edits] + ([propose_note_turn] if note else [])
    return ScriptedModel(turns + [say(f'Report: one rule, one {edit} edit' + (' and one note' if note else '') + ' proposed.')])
