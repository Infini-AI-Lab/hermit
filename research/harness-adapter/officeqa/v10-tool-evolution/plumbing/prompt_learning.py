"""v10 prompt learner (ArmSpec.learner 'text': arms `prompt-evolve` and `harness-evolve-notes`; DESIGN §5).

The v9 text learner under its v10 name: scoped notes only, on either adapter architecture. On a prompt
adapter (SEED.md + notes/) it is the `prompt-evolve` learner; on a harness adapter it is the notes-only
control of the tool-evolving learner (`harness-evolve-notes`: same W0, tools, controls, SEED and USAGE.md as
`harness-evolve`, only the tool proposals missing). Per block the learner (same model, reasoning effort, budget,
evidence and domain text as the harness learner; learner_common.run_round) may propose notes with
`propose_note`; a note enters the next revision only after paired actor re-runs (cards machinery: support x2
+ 2 matched controls x2, cards block.judge), and is retired later when its prequential accuracy falls >= 20
points below the same blocks' episodes without it (learner_common.retirement). PREREG calls this
"scoped-note evolution (paired-rerun admitted)", not GEPA/SkillOpt: the seed text is never rewritten.

Extra reads (v10): `read_adapter_file`, `search_adapter_file` and `read_tool_output` are common tools; on a
harness adapter this learner reads the same files as the harness learner (tools/*.py, USAGE.md, manifest.json,
controls.json, read-only) and sees the same tool evidence section (learner_common.tools_evidence_section), so
the harness-evolve vs harness-evolve-notes contrast differs only in the proposals a learner may make (review
fix). On a prompt adapter every proposed note is also refused when it names one of the harness tools
(learner_common.screen_note; DESIGN §3): they do not exist in that workspace.

Entry point (v8 learner contract + injected actor runner):
    run_learning(key, source, evidence, output, block, stop=None, *, actor_runner, **options) -> result dict
`options` are learner_common.run_round keywords (pack, grader, model, corpus, root, w0, budget,
reasoning_effort, exclude, concurrency, arm); production passes actor_runner (+ corpus / w0 / arm as needed).
"""
from __future__ import annotations

import threading

from harness_core import contracts as C

import learner_common as LC

PROMPT_SECTION = f'''## Your task: scoped notes for the actor (notes-only learner)
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
- The seed text and the tools are fixed for you: you may read them (read_adapter_file, search_adapter_file; on a
  harness adapter also tools/*.py, tools/USAGE.md, tools/manifest.json) but not change them.
End with a short report (causes found, notes proposed, open leads) and no tool call.
'''
PROMPT_ADAPTER_NOTE = '''The actor of this adapter has no table tools (only the search helper, rg, sed and python): a note
must not name find_tables, show_table, printings or calc; the screen refuses it.
'''
NUDGE = ('You ended without proposing a note. If one note would have changed the outcome of one or more pool '
         'questions (name only questions it really decides), propose it now with propose_note; otherwise reply '
         '"no note" and explain why in one paragraph.')


def prompt_section(ctx: LC.RoundContext) -> str:
    return PROMPT_SECTION + (PROMPT_ADAPTER_NOTE if ctx.architecture == 'prompt' else '')


SPEC = LC.LearnerSpec(name='text', architectures=('prompt', 'harness'), profile='prompt-evolve',
                      extra_tools=lambda ctx: [], prompt_section=prompt_section, nudge=NUDGE,
                      adapter_reads=('SEED.md', 'notes/*.json', 'tools/*.py', 'tools/USAGE.md', 'tools/manifest.json',
                                     'controls.json'),
                      stages_tools=False)


def run_learning(key, source, evidence, output, block, stop: threading.Event | None = None, *, actor_runner, **options):
    return LC.run_round(SPEC, key, source, evidence, output, block, stop, actor_runner=actor_runner, **options)
