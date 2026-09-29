"""Scripted learner models for the toy campaign (tests/fake_learner_model.ScriptedModel; $0, no network).

Both read the released listing first (`list_episodes`) and base every proposal on what it shows (wrong
episodes = the pool), so their proposals are a deterministic function of the evidence:
- `prompt_model` (the notes-only learner: arms `prompt-evolve` and `harness-evolve-notes`): one scoped note for
  the first failure class it recognises among the wrong episodes, with support = the wrong episodes whose
  question contains the note's trigger;
- `harness_model` (arm `harness-evolve`): one tool revision, a patch to `tools/lookup.py` that prints a hint
  line ('lookup: revised printing exists: <file>') above the cells of a first-printing 2020 file (additive: it
  adds one line, changes none), with `expect` = the trajectory steps of the first-printing lookups of its
  support episodes (read with `read_episode`), and, with `note=True`, the same scoped note as the prompt
  learner on the remaining wrong first-printing episode (a separate note, so the two proposals are verified
  on their own support questions). With one wrong first-printing episode only the revision is proposed.
The note text and the hint line carry the phrases the weak actor reacts to (tests/e2e/weak_actor.py), so
paired re-run verification through the campaign's actor runner flips the support questions.
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
FIRST_PRINTING_TRIGGER = NOTE_CLASSES[0][1]
LINE = re.compile(r'^q(?P<o>\d{3}) b(?P<b>\d+) strict (?P<s>\d) 1% \d (?P<status>\S+)(?P<lost> transport-lost)? \| Q: (?P<q>.*?) \|')
# the trajectory line of a first-printing lookup: '[#N] COMMAND (exec_command): lookup widgets_2020_a.txt ...'
FIRST_PRINTING_STEP = re.compile(r'\[#(\d+)\] COMMAND \([^)]*\): lookup widgets_2020_a\.txt')
# The tool patch (tests/e2e; DESIGN §5 stage_tool_patch: exact, unique replacement in tools/lookup.py, a copy of
# packs/toy/tools.py). Additive: one hint line above the cells of a first-printing file that has a revised
# printing; the no-match output and every cell line stay as they were. The new string literal is < 64 chars.
PATCH_PATH = 'tools/lookup.py'
PATCH_OLD = "    return '\\n'.join(out) if out else 'lookup: no matching rows'\n"
PATCH_NEW = ("    revised = file.replace('_a.txt', '_b.txt')\n"
             "    hint = ['lookup: revised printing exists: ' + revised] if revised != file and revised in FILES else []\n"
             "    return '\\n'.join(hint + out) if out else 'lookup: no matching rows'\n")
HINT = 'lookup: revised printing exists: '
REVISION_ID = 'revised_hint'


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


def all_listing(messages) -> list[dict]:
    """The listing wherever it is in the conversation (later turns have other tool results in between)."""
    rows = []
    for message in messages:
        if message.get('role') == 'tool':
            try:
                value = json.loads(message['content'])
            except ValueError:
                continue
            if isinstance(value, dict) and value.get('part') == 'list':
                rows = listing([{'role': 'assistant'}, message])
    return rows


def wrong_episodes(rows) -> list[dict]:
    return [r for r in rows if not r['strict'] and not r['lost']]


def note_proposal(rows, *, support_ordinals=None) -> dict | None:
    wrong = wrong_episodes(rows)
    for note_id, trigger, text in NOTE_CLASSES:
        support = [r['ordinal'] for r in wrong if trigger in r['question']][:3]
        if support_ordinals is not None:
            support = [o for o in support if o in support_ordinals]
        if support:
            return {'id': note_id, 'triggers': [trigger], 'text': text, 'support': support,
                    'rationale': f'the wrong episodes {support} share this failure; the note names the fix'}
    return None


def prompt_model() -> ScriptedModel:
    def propose(messages):
        proposal = note_proposal(listing(messages))
        if proposal is None:
            return say('No wrong released episode matches a failure I can address; no proposal.')
        return calls(tool_call('propose_note', **proposal))
    return ScriptedModel([calls(tool_call('list_episodes')), propose,
                          say('Report: one scoped note proposed for the wrong episodes of this block.')])


text_model = prompt_model          # the v9 name


def harness_model(*, note: bool = True) -> ScriptedModel:
    """One tool revision (+ one separate note when `note` and two wrong first-printing episodes exist)."""
    state: dict = {}

    def read_support(messages):
        rows = listing(messages)
        wrong = [r['ordinal'] for r in wrong_episodes(rows) if FIRST_PRINTING_TRIGGER in r['question']]
        if not wrong:
            return say('No wrong released episode read a first printing; no tool change is warranted.')
        if note and len(wrong) >= 2:
            state['revision_support'], state['note_support'] = wrong[:1], wrong[-1:]
        else:
            state['revision_support'], state['note_support'] = wrong[:3], []
        return calls(*[tool_call('read_episode', ordinal=o, part='trajectory') for o in state['revision_support']])

    def stage(messages):
        if 'revision_support' not in state:
            return say('done')
        expect = {}
        for result in tool_results(messages):
            if result.get('part') == 'trajectory':
                steps = [int(m.group(1)) for m in FIRST_PRINTING_STEP.finditer(result['content'])]
                if steps:
                    expect[str(result['ordinal'])] = steps[:1]
        state['expect'] = expect
        return calls(tool_call('stage_tool_patch', path=PATCH_PATH, old=PATCH_OLD, new=PATCH_NEW))

    def propose(messages):
        if 'expect' not in state:
            return say('done')
        support = state['revision_support']
        return calls(tool_call('propose_revision', id=REVISION_ID, support=support,
                               rationale=f'the wrong episodes {support} read the first printing of a 2020 file; lookup now '
                                         'says that a revised printing exists (an added line, no value changes)',
                               expect=state['expect']))

    def propose_note(messages):
        proposal = note_proposal(all_listing(messages), support_ordinals=state.get('note_support') or [])
        if proposal is None:
            return say('No separate note: every wrong first-printing episode is the revision\'s support.')
        return calls(tool_call('propose_note', **proposal))

    turns = [calls(tool_call('list_episodes')), read_support, stage, propose] + ([propose_note] if note else [])
    return ScriptedModel(turns + [say('Report: one tool revision' + (' and one scoped note' if note else '') + ' proposed.')])
