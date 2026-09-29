"""v10 harness learner (ArmSpec.learner 'harness': arm `harness-evolve`; DESIGN §5).

Notes and tool revisions on a harness adapter. Everything shared with the notes-only learner
(evidence, model, budget, common tools, note screen and verification, retirement) is in learner_common; this
module adds the tool-side evidence and the tool-side proposals:
- evidence for tools (DESIGN §5 "Learner evidence for tools" (a)-(d)) is learner_common.tools_evidence_section,
  shown identically to this learner and to the notes-only learner on a harness adapter (review fix: the d vs e
  contrast differs only in the proposals a learner may make): exit codes on every OUTPUT line and
  `read_tool_output`; the friction digest (evidence.friction_digest); the failure-stage taxonomy mapped to the
  tool or text that owns it; tools/USAGE.md, the manifest, the diff vs W0 and the W0 tool changes already made
  from the analysis (`pack.w0_changes_text()`); this module adds (e), the guidance on which edits pass;
- tools: the common read tools (read_adapter_file over tools/*.py, USAGE.md, manifest.json, controls.json;
  search_adapter_file) plus stage_tool_patch, stage_tool_file, try_tool, propose_revision
  (learner_common.harness_tools), beside propose_note. The screens, the replay, the tiers, the control choice and the paired re-run admission are
  learner_common's (`propose_revision`, `verify_revisions`).
SEED.md, controls.json and calc.py are fixed this round; `propose_revert` and the prequential monitoring of
revisions are deferred (DESIGN scope decision 2).

Entry point (v8 learner contract + injected actor runner):
    run_learning(key, source, evidence, output, block, stop=None, *, actor_runner, tool_runner, **options)
`tool_runner` maps a tools directory to a harness_core.runner.ToolRunner (campaign.tool_runner builds it in
one place: candidate / source tools/, prepared/agent/corpus, prepared/agent/tables/tables.sqlite,
evolution/BB/tmp); `options` are learner_common.run_round keywords (pack, grader, model, corpus, root, w0,
budget, reasoning_effort, exclude, concurrency, arm).
"""
from __future__ import annotations

import threading

from harness_core import contracts as C

import learner_common as LC

PROMPT_SECTION = f'''## Your task: scoped notes and tool revisions (harness learner)
The actor's instructions always contain the seed text (fixed) and learned notes (by trigger, at most
{C.NOTES_PER_EPISODE} per question); its README carries tools/USAGE.md; its shell has the adapter tools listed in
tools/manifest.json. You may add notes (propose_note, as the notes-only learner) and change the tools:
- read_adapter_file(path, start_line, num_lines) and search_adapter_file(path, text) read tools/*.py,
  tools/USAGE.md, tools/manifest.json, SEED.md, notes/<id>.json and controls.json (with your staged edits);
- stage_tool_patch(path, old, new) replaces one exact, unique substring of a candidate tools/<module>.py
  (Edit semantics: old must occur once; at most {LC.PATCHES_MAX} patches per revision); stage_tool_file(path,
  content) adds a new tools/<module>.py (<= {C.TOOL_NEW_FILE_MAX} bytes) or replaces tools/USAGE.md or
  tools/manifest.json. Every staged file is screened at once (python compiles and imports resolve under the
  actor python; exec/eval/__import__/compile, os.fork/exec/system and the network or process modules are refused;
  new code must not carry question ids, ordinals, file names, question wording, released answer values - a new
  constant or constant expression that agrees with a released answer to 3 digits, a new list or dict of numbers,
  a data-like string literal over {LC.STRING_LITERAL_MAX} characters (mostly digits or numbers) or a base64 token is
  refused - ordinary one-line messages are fine; the manifest and
  USAGE.md rules; the {C.TEXT_BUDGET}-byte text budget). tools/calc.py, SEED.md and controls.json are fixed this
  round.
- try_tool(argv, stdin) runs a candidate tool over the real corpus (<= {LC.TRY_TOOL_MAX} calls, {LC.REPLAY_JOB_SECONDS} s each).
- propose_revision(id, support, rationale, expect, note): bundles the staged files (+ an optional note) into one
  proposal. Before it is accepted, every recorded manifest-tool command of the support episodes and the most
  recent commands of the other released episodes are replayed with the old and the new tools: a command that
  worked and now fails, a broken show_table/printings first line, a change that alters none of the commands you
  name in `expect` (COMMAND or OUTPUT step numbers), or an undocumented new option refuses the proposal (the reply
  says why; you may fix the files and propose again, at most {LC.REPLAYS_MAX} replays; a USAGE.md-only
  proposal needs none). A brand-new tool alters no recorded command, so it can only pass bundled with a change
  to an existing tool's output (for example a hint line that points at it). The replay tiers the change -
  additive (only added output lines: {LC.KEEP_ADDITIVE} controls) or value-changing ({LC.KEEP_VALUE_CHANGING}
  controls, a control whose output changed must stay right in every run).
- Slots per block: at most {LC.B_MAX_NOTES} notes + {LC.B_MAX_REVISIONS} revision (a revision's bundled note uses
  the revision slot). Verification after this session: support questions x{LC.K} and controls x{LC.K} with the
  candidate tools; one support question must be right in every run (each further support allows one miss) and no
  control may be lost.

## Guidance for tool revisions
Additive output changes (an extra line, a hint, a label) and option aliases are the edits most likely to pass
the replay and to generalise; a change that alters existing values needs a control question that exercised it
and must name any right episode whose calc inputs it alters on purpose in the rationale. The evidence for
tools ((a)-(d) below) is the same the notes-only learner sees on this adapter.
End with a short report (causes found, notes and revisions proposed, open leads) and no tool call.
'''
NUDGE = ('You ended without proposing anything. If one note or one tool revision would have changed the outcome of '
         'one or more pool questions (name only questions it really decides), propose it now; otherwise reply '
         '"no proposal" and explain why in one paragraph.')

tools_diff_vs_w0 = LC.tools_diff_vs_w0        # the diff both learners see (learner_common.tools_evidence_section)


def status_section(ctx: LC.RoundContext) -> str:
    """The harness learner's own status: its staged, unproposed files (the tool evidence (a)-(d), manifest,
    USAGE.md, diff vs W0, W0 changes and friction digest are learner_common.tools_evidence_section, shared
    with the notes-only learner on a harness adapter)."""
    if not ctx.staged:
        return ''
    return '## Staged (unproposed) tool files\n' + ', '.join(sorted(ctx.staged)) + '\n\n'


SPEC = LC.LearnerSpec(name='harness', architectures=('harness',), profile='harness-evolve',
                      extra_tools=lambda ctx: LC.harness_tools(), prompt_section=lambda ctx: PROMPT_SECTION,
                      status_section=status_section, nudge=NUDGE,
                      adapter_reads=('SEED.md', 'notes/*.json', 'tools/*.py', 'tools/USAGE.md', 'tools/manifest.json',
                                     'controls.json'),
                      stages_tools=True)


def run_learning(key, source, evidence, output, block, stop: threading.Event | None = None, *, actor_runner,
                 tool_runner, **options):
    return LC.run_round(SPEC, key, source, evidence, output, block, stop, actor_runner=actor_runner,
                        tool_runner=tool_runner, **options)
