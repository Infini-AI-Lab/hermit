"""v9 text learner (arm `text-evolve`; DESIGN §1, §3; validity review P3).

The text adapter = W0 seed text (SEED.md: the core advice + pack.seed_text(), always shown) + learned scoped
notes (notes/<id>.json, contracts.NoteSpec; at most NOTES_PER_EPISODE rendered per episode by trigger).
Per block the learner (same model, reasoning effort, budget, evidence and domain text as the mechanism
learner; learner_common.run_round) may propose notes with `propose_note`; a note enters the next revision
only after paired actor re-runs (cards machinery: support x2 + 2 matched controls x2, cards block.judge),
and is retired later when its prequential accuracy falls >= 20 points below the same blocks' episodes
without it (learner_common.retirement). The learner writes no file; the v8 prompt learner's file-tool and
ADAPTER.md growth problems cannot occur.

Entry point (v8 learner contract + injected actor runner):
    run_learning(key, source, evidence, output, block, stop=None, *, actor_runner, **options) -> result dict
`options` are learner_common.run_round keywords (pack, grader, model, corpus, root, w0, budget,
reasoning_effort, exclude, concurrency); production passes only actor_runner (+ corpus / w0 as needed).
"""
from __future__ import annotations

import threading

from harness_core import contracts as C

import learner_common as LC

PROMPT_SECTION = f'''## Your task: scoped notes for the actor (text adapter)
The actor's instructions always contain the seed text; learned notes are added per question: a note is shown only on
questions whose wording contains one of its triggers (lowercase keywords or short phrases, whole words), at most
{C.NOTES_PER_EPISODE} notes per question. Write a note for a family of questions, not for one question.
- propose_note(id, triggers, text, support, rationale): at most {LC.B_MAX_NOTES} accepted per block; text at most
  {C.NOTE_MAX} bytes of concrete procedure (where to look, what to compare, which convention decides).
- support: {LC.SUPPORT_MIN}-{LC.SUPPORT_MAX} pool questions this note would have fixed; each must contain a trigger.
- Verification after this session: the actor is re-run with the note {LC.K} times on each support question and
  {LC.KEEP} matched control questions it answered right; the note enters only if one support question is right in
  every run (each further support question allows one miss) and no control is lost.
- A note whose later questions do clearly worse than the same blocks' questions without it is retired.
End with a short report (causes found, notes proposed, open leads) and no tool call.
'''
NUDGE = ('You ended without proposing a note. If one note would have changed the outcome of one or more pool '
         'questions (name only questions it really decides), propose it now with propose_note; otherwise reply '
         '"no note" and explain why in one paragraph.')

SPEC = LC.LearnerSpec(name='text', architecture='text', profile='text-evolve', extra_tools=lambda ctx: [],
                      prompt_section=lambda ctx: PROMPT_SECTION, nudge=NUDGE)


def run_learning(key, source, evidence, output, block, stop: threading.Event | None = None, *, actor_runner, **options):
    return LC.run_round(SPEC, key, source, evidence, output, block, stop, actor_runner=actor_runner, **options)
