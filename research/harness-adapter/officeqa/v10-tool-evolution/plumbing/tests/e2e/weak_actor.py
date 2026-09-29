"""Scripted weak actor model for the toy campaign, served by tests/fake_upstream.py behind the real broker.

`respond(body, index)` is a pure function of the Responses request body (what the model sees), so one fake
upstream serves every arm, every root and every episode (scored or verification) concurrently. It behaves like
a weak actor:
- reads README.md first (as the prompt says) and learns from it which tools exist (`lookup`, `calc`,
  `submit`) and where the corpus is; the plain arm, without pack tools, uses `rg` and `python3 -c` instead;
- makes the typical toy mistakes (first printing, percent for a decimal, one value for two, the wrong
  quarter's price), repeats one lookup (a loop: 5 times, or until a `[runtime:loop_guard]` note appears),
  answers two questions without `submit`, submits the others, and on a gate finding accepts the offer, fixes
  the answer or keeps it (per question; on toy07 its first --keep reason is too long, so the gate rejects that
  payload and it keeps again with a short reason);
- reacts to a scoped note only through its content: a note that says the 2020 figures were revised makes it
  read the revised printing; a note about decimals makes it answer the decimal; a note about 'both' makes it
  give both values; and to a tool output only through its content: a `lookup` output that says a revised
  printing of the file exists (the evolved toy tool of tests/e2e/learners.py) makes it read that printing
  too, unless the question pins the first printing (v10: the model sees only text, notes or tool outputs);
- with the submit gate off (every v10 arm this round) README has no submit section, so it answers directly;
  the loop note wording of the gate-off controller stops its loops like the gate-on one;
- on toy08 it reports an inflated usage (250,000 input tokens) once, so Codex compacts (explicit limit
  244,800); the compaction reply is a summary carrying a `STATE {...}` line, from which it resumes.
Nothing here reads a reference answer: the values come from the toy corpus, like a model reading tables.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
import shlex

import fake_upstream as fu
from packs.toy import corpus as TC
from packs.toy.items import ITEMS

QUESTIONS = {item.question: item.item_id for item in ITEMS}
RESUME_PREFIX = 'Another language model started to solve this problem'
STATE = 'STATE '
BIG_USAGE = 250_000              # above the explicit model_auto_compact_token_limit=244800 -> one compaction
LOOP_MAX = 5                     # a looping command is repeated this often unless a loop note stops it
NOTES_HEADING = '## Notes for this question'
HINT = 'revised printing exists'     # printed by the evolved toy lookup (tests/e2e/learners.py PATCH_NEW)
# A curious model looking around (toy06, every arm): the episode's run records, the root's state and the answer
# key sit above the workspace; the v9 Seatbelt profile must deny all of it (the run records name the arm).
PROBE = ('ls .. ; cat ../records/invocation.json ; cat ../../../../state.json ; '
         'cat ../../../../prepared/private/answers.jsonl ; cat .acting-prepared.json')


def raw(row: str, period: str, printing: str | None = None, table: str = 'W-1') -> str:
    """The number as the bulletin prints it (revision marker dropped): what a model copies into code."""
    return TC.find(row, period, printing=printing, table=table).raw.rstrip('r')


def num(text: str) -> float:
    return float(text)

# ---------------------------------------------------------------------------------------------
# What the model sees
# ---------------------------------------------------------------------------------------------


def texts(item: dict) -> list[str]:
    return [p.get('text') or '' for p in item.get('content') or [] if isinstance(p, dict)]


@dataclass
class View:
    question: str = ''
    item: str = ''
    agents: str = ''
    notes: str = ''                                # the rendered notes section of AGENTS.md ('' when none)
    done: list[str] = field(default_factory=list)  # commands run so far (summary STATE + this window)
    outputs: list[tuple[str, str]] = field(default_factory=list)   # (cmd, output) in this window
    harness: list[str] = field(default_factory=list)               # '[runtime:...]' user messages
    readme: dict = field(default_factory=dict)     # {'tools', 'calc', 'submit', 'corpus'}
    last_gate: str = ''                            # the last submit output (survives compaction via STATE)
    loop_noted: bool = False
    summary: bool = False
    last: dict = field(default_factory=dict)


def parse(body: dict) -> View:
    view = View()
    calls: dict[str, str] = {}
    for item in body.get('input') or []:
        kind = item.get('type')
        if kind == 'message' and item.get('role') == 'user':
            for text in texts(item):
                if text.startswith('# AGENTS.md instructions'):
                    view.agents = text
                    if NOTES_HEADING in text:
                        view.notes = text.split(NOTES_HEADING, 1)[1].split('</INSTRUCTIONS>')[0]
                elif 'Question:\n' in text:
                    view.question = text.split('Question:\n', 1)[1].strip()
                elif text.startswith('[runtime:'):
                    view.harness.append(text)
                    if text.startswith('[runtime:loop_guard]'):
                        view.loop_noted = True
                elif text.startswith(RESUME_PREFIX):
                    view.summary = True
                    match = re.search(STATE + r'(\{.*\})', text)
                    if match:
                        state = json.loads(match.group(1))
                        view.done += state['done']
                        view.readme = state['readme']
                        view.last_gate = state['last_gate']
                        view.loop_noted = view.loop_noted or state['loop_noted']
        elif kind == 'function_call':
            try:
                cmd = json.loads(item.get('arguments') or '{}').get('cmd', '')
            except ValueError:
                cmd = ''
            calls[item.get('call_id')] = cmd
            view.done.append(cmd)
        elif kind == 'function_call_output':
            cmd = calls.get(item.get('call_id'), '')
            output = str(item.get('output') or '').split('Output:\n', 1)[-1]
            view.outputs.append((cmd, output))
            if cmd == 'cat README.md':
                view.readme = {'tools': 'lookup <file>' in output, 'calc': "calc -c '<python>'" in output,
                               'submit': '# Submitting your answer' in output,
                               'corpus': output.split('The text corpus is read-only at:\n', 1)[1].split('\n')[0].strip()}
            if cmd.startswith('submit'):
                view.last_gate = output
    view.item = QUESTIONS.get(view.question, '')
    items = body.get('input') or []
    view.last = items[-1] if items else {}
    return view

# ---------------------------------------------------------------------------------------------
# The weak actor's plan per question
# ---------------------------------------------------------------------------------------------


@dataclass
class Plan:
    steps: list[tuple[str, str]]          # ('run' | 'loop', command)
    answer: str
    submit: bool = True                   # controller arms: False = answer without submit (enforcement path)
    on_finding: str = 'accept'            # accept | keep | fix
    fix: str = ''
    alt: str = ''
    big_usage_on: str = ''                # the command whose response reports BIG_USAGE (toy08)
    long_keep: bool = False               # the first --keep reason exceeds the gate's 500-character limit


def plan_for(view: View) -> Plan:
    r = view.readme
    tools = bool(r.get('tools'))
    corpus = r.get('corpus', '')
    notes = view.notes.lower()
    hinted = any(HINT in output for cmd, output in view.outputs if cmd.startswith('lookup'))
    revised = ('revised printing' in notes or hinted) and 'first printed' not in view.question
    decimal = 'as a decimal' in notes
    both = 'both values' in notes

    def lookup(file, *rows, table=None):
        if tools:
            return f'lookup {file} {" ".join(rows)}'.rstrip() + (f' --table {table}' if table else '')
        pattern = '^\\| (' + '|'.join(rows or ('Price',)) + ') '
        return f'rg -n {shlex.quote(pattern)} {shlex.quote(corpus + "/" + file)}'

    def calc(code):
        return (f"calc -c '{code}'" if r.get('calc') else f"python3 -c '{code}'")
    readme = ('run', 'cat README.md')
    item = view.item
    if item == 'toy01':                   # first printing (later_printing offers the revised sum; accepted)
        p = 'b' if revised else 'a'
        a, b = raw('North', 'Q1 2020', p), raw('North', 'Q2 2020', p)
        return Plan([readme, ('run', lookup(f'widgets_2020_{p}.txt', 'North')),
                     ('run', calc(f'print(round({a} + {b}, 2))'))], f'{num(a) + num(b):.2f}')
    if item == 'toy02':                   # right; loops on the lookup; --alt from the first printing, kept
        a, b = raw('South', 'Q1 2020', 'b'), raw('South', 'Q4 2020', 'b')
        first = raw('South', 'Q1 2020', 'a')
        return Plan([readme, ('loop', lookup('widgets_2020_b.txt', 'South')),
                     ('run', calc(f'print(({b} - {a}) / {a} * 100)'))],
                    f'{(num(b) - num(a)) / num(a) * 100:.2f}', on_finding='keep',
                    alt=f'{(num(b) - num(first)) / num(first) * 100:.2f}')
    if item == 'toy03':                   # percent for a decimal (scale offers the decimal; accepted)
        a, b = raw('East', 'Q1 2021'), raw('East', 'Q4 2021')
        rate = (num(b) - num(a)) / num(a)
        return Plan([readme, ('run', lookup('widgets_2021_a.txt', 'East')),
                     ('run', calc(f'r = ({b} - {a}) / {a}; print(r); print(r * 100)'))],
                    f'{rate:.4f}' if decimal else f'{rate * 100:.2f}')
    if item == 'toy04':                   # right; answered without submit (enforcement check is clean)
        values = [raw('East', f'Q{q} 2020', 'b') for q in range(1, 5)]
        return Plan([readme, ('run', lookup('widgets_2020_b.txt', 'East')),
                     ('run', calc(f'print(({" + ".join(values)}) / 4)'))],
                    f'{sum(map(num, values)) / 4:.1f}', submit=False)
    if item == 'toy05':                   # one value for two (answer_count, no offer; the model fixes it)
        a, b = raw('West', 'Q3 2020', 'a'), raw('West', 'Q3 2021')
        return Plan([readme, ('run', lookup('widgets_2020_a.txt', 'West')), ('run', lookup('widgets_2021_a.txt', 'West'))],
                    f'{a}, {b}' if both else a, on_finding='fix', fix=f'{a}, {b}')
    if item == 'toy06':                   # right; the question pins the first printing; probes the sandbox first
        a, b = raw('North', 'Q1 2020', 'a'), raw('South', 'Q1 2020', 'a')
        return Plan([readme, ('run', PROBE), ('run', lookup('widgets_2020_a.txt', 'North', 'South')),
                     ('run', calc(f'print({a} / {b})'))], f'{num(a) / num(b):.3f}')
    if item == 'toy07':                   # first-printing total (later_printing offer; the weak model keeps it)
        p = 'b' if revised else 'a'
        a, b = raw('Total', 'Q2 2020', p), raw('Total', 'Q2 2021')
        return Plan([readme, ('run', lookup(f'widgets_2020_{p}.txt', 'Total')), ('run', lookup('widgets_2021_a.txt', 'Total')),
                     ('run', calc(f'print(round({b} - {a}, 2))'))], f'{num(b) - num(a):.2f}', on_finding='keep',
                    long_keep=True)
    if item == 'toy08':                   # right; plain look-up; forces one compaction
        cmd = lookup('widgets_2021_a.txt', 'Price', table='W-2')
        return Plan([readme, ('run', cmd)], f'{num(raw("Price", "Q3 2021", table="W-2")):.2f}', big_usage_on=cmd)
    if item == 'toy09':                   # the Q2 price for Q1 (period_match offers the Q1 price; accepted); loops
        a, b = raw('East', 'Q1 2021'), raw('Price', 'Q2 2021', table='W-2')
        return Plan([readme, ('loop', lookup('widgets_2021_a.txt', 'East', 'Price')),
                     ('run', calc(f'print(round({a} * {b}))'))], f'{round(num(a) * num(b))}')
    if item == 'toy10':                   # right, typed without calc; not submitted (enforcement finding, kept)
        a, b = raw('West', 'Q1 2021'), raw('West', 'Q4 2021')
        return Plan([readme, ('run', lookup('widgets_2021_a.txt', 'West'))],
                    f'{(num(b) - num(a)) / num(a):.4f}', submit=False, on_finding='keep')
    return Plan([readme], 'unknown')

# ---------------------------------------------------------------------------------------------
# One step
# ---------------------------------------------------------------------------------------------


def next_command(plan: Plan, view: View) -> str | None:
    """The first plan step not yet done (a 'loop' step repeats until LOOP_MAX or a loop note)."""
    done = list(view.done)
    for kind, cmd in plan.steps:
        if kind == 'loop':
            count = done.count(cmd)
            if count == 0 or (count < LOOP_MAX and not view.loop_noted):
                return cmd
        elif cmd not in done:
            return cmd
    return None


def recorded(text: str) -> str | None:
    match = re.search(r'recorded "((?:[^"\\]|\\.)*)"', text)
    return match.group(1) if match else None


def offer(text: str) -> str | None:
    match = re.search(r'Accept: submit --answer "((?:[^"\\]|\\.)*)"', text)
    return match.group(1) if match else None


def submitted(cmd: str) -> str:
    return shlex.split(cmd)[2] if cmd.startswith('submit --answer') else ''


def usage(big: bool = False):
    return fu.usage(BIG_USAGE if big else 1_200, 40)


def summary_reply(view: View):
    state = {'done': view.done, 'readme': view.readme, 'last_gate': view.last_gate, 'loop_noted': view.loop_noted}
    text = (f'Handoff summary: question {view.item or "?"}; commands run so far are listed below.\n'
            f'{STATE}{json.dumps(state, ensure_ascii=False)}')
    return fu.response([fu.message(text)], use=usage())


def respond(body: dict, index: int):
    view = parse(body)
    if not body.get('tools'):                       # Codex's compaction request (tools=[] + checkpoint prompt)
        return summary_reply(view)
    plan = plan_for(view)
    last_text = ' '.join(texts(view.last)) if view.last.get('type') == 'message' else ''
    if last_text.startswith('[runtime:enforce]'):   # the broker's enforcement re-query of an unsubmitted answer
        alt = offer(last_text)
        return fu.response([fu.message(alt if alt and plan.on_finding == 'accept' else plan.answer)], use=usage())
    command = next_command(plan, view)
    if command is not None:
        big = bool(plan.big_usage_on) and command == plan.big_usage_on and not view.summary
        return fu.response([fu.call(command)], use=usage(big))
    if not (view.readme.get('submit') and plan.submit):
        return fu.response([fu.message(plan.answer)], use=usage())
    submits = [cmd for cmd in view.done if cmd.startswith('submit')]
    if not submits:
        extra = f' --alt "{plan.alt}"' if plan.alt else ''
        return fu.response([fu.call(f'submit --answer "{plan.answer}"{extra}')], use=usage())
    current = submitted(submits[-1]) or plan.answer
    gate = view.last_gate
    kept = recorded(gate)
    if kept is not None or len(submits) >= 4:
        return fu.response([fu.message(kept if kept is not None else current)], use=usage())
    alternative = offer(gate)
    if plan.on_finding == 'accept' and alternative:
        return fu.response([fu.call(f'submit --answer "{alternative}"')], use=usage())
    if plan.on_finding == 'fix' and plan.fix and plan.fix != current:
        return fu.response([fu.call(f'submit --answer "{plan.fix}"')], use=usage())
    reason = 'I checked the bulletin I read'
    if plan.long_keep and not any('--keep' in cmd for cmd in submits):
        reason = ' '.join([reason + '.'] * 20)          # > 500 characters: the gate rejects the payload (not a round)
    return fu.response([fu.call(f'submit --answer "{current}" --keep "{reason}"')], use=usage())
