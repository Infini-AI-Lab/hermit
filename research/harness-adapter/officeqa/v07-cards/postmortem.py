"""Controller-side post-mortem: block grades, learner phases, lesson replay.

Grades and failure records are released to the learner for the sealed block
only. Lessons are served to later actors only while their `check:` list
replays green here; the controller never runs learner Python.
"""
import json
from pathlib import Path
import re

from officeqa_prepare import load_grader, sha256
from officeqa_tools import OfficeQACorpus
from paths import VENDOR

STEPS = ('S1', 'S2', 'S3', 'S4', 'S5', 'S6', 'S7')
LADDER = '''Ladder (name the FIRST rung that failed):
S1 bulletin file / vintage: compare the same row across later vintages; annual rows take the latest "final" vintage, monthly rows the first printing, unless the question says otherwise.
S2 table / section.  S3 row, column, units (millions vs billions, fiscal vs calendar, percent vs percentage points).
S4 extract cells: count them.  S5 formula / convention: write the convention next to the number, enumerate alternatives, match the question wording.
S6 format / round: round last; the strict grader has tolerance 0.  S7 harness / runtime cause (transport, output limit, loop, no completed turn).'''

GOAL_COMMON = '''Post-mortem learning session for the five just-completed adapted-arm episodes.
Released for THIS block only: each question, the sealed submission, the official reference
(evidence/<nnn>/reference_answer.json), the strict and 1% grader verdicts and the failure
record (evidence/<nnn>/failure.json; status, terminal_event, error_text, requests,
wire_attempts, call_failures, served AGENTS.md bytes). Nothing about future questions exists
anywhere you can read. Grades of unreleased batches are never shown.

''' + LADDER + '''

Tools. Session files: list_files(area), read_file(area,path,offset), write_file(area,path,content);
areas adapter, evidence, scratch (history, method, environment read-only). Environment paths listed
below: read_path(path), list_path(path), or run_command(command) (your own python, grep -n, sqlite3;
no network; a command ends after 600 s). Controller checkers, the only proofs the replay accepts:
  verify_cell(file, line, contains) -> {found, line_text, neighbours}: one corpus TXT line.
  verify_answer(ordinal, candidate) -> {strict, one_percent}: grades a candidate against the
  released reference of THIS block; other ordinals are refused.
Reference shelf (when a path is given): extracted reference text, learner-only. Use
run_command('grep -n "term" <shelf>/*') when a formula or convention is suspect. It is
information only, never a proof: a shelf claim still needs a verify_cell or verify_answer check.

Phases. The controller sends one phase per failed episode, then a serving phase. In each phase:
(a) DIAGNOSE with new information: trajectory.jsonl, failure.json, the corpus through
    verify_cell/run_command, the shelf through grep -n.
(b) DECOMPOSE the attempt into the ladder rungs.
(c) VERIFY every rung now: verify_cell for data rungs (S1-S4), your own python via run_command
    for S5-S6 arithmetic, verify_answer for the final rung until strict==1, or mark the chain
    open at the last passing rung.
(d) RECORD: write_file(area="scratch", path="postmortem/<nnn>.json", content=...) with content =
    {"ordinal":n,"failed_step":"S3","cause":"...","sources":["file.txt:line"],
     "substeps":[{"step":"S4","claim":"...","check":{...},"result":"pass|fail"}],"lessons":["L-..."]}
    and one lesson per re-verified rung with its check list copied in (format below).
Then stop with no tool call: that ends the phase and the next one starts. Never end a phase
while a tool call is pending. Do not write lookup tables of question ids or answers; lessons
are reusable table families, conventions and harness rules.

Lesson record, identical key: value lines in every lesson:
status: verified|candidate      (write candidate; the controller sets verified after replay)
scope: table:<family>|convention:<name>|harness
step: S1..S7
check: [{"kind":"cell","file":"<corpus .txt>","line":<n>,"contains":"<text>"},{"kind":"answer","ordinal":<n>,"candidate":"<final answer>"}]
verified_by: postmortem:<nnn>
brief: <=300 chars, the rule itself
A lesson with an empty or failing check list stays candidate and is not served. Answer checks
must name a released ordinal (this block or earlier); cell checks name one corpus line.
'''

GOAL_MWM = '''MWM adapter. Lessons live at memory/L-<slug>.md (id = file stem) with the usual header
(last_confirmed_t, scope, derived_from, brief) plus the status/step/check/verified_by lines, then
the body. derived_from names the examples/<file>.md you wrote for the episode or "none (reasoning
only)"; the record address goes in verified_by. Only status: verified lessons are listed in the
served lessons index; the controller
replays every check list each block and rewrites status. All native layers stay open: memory,
examples, consumption/brief.md (advisory 30,000 chars; nothing is cut), tested Python tools
(test_python/captest), agent roles. Keep other memory entries and the briefing consistent with
the lessons. Follow CONTRACT.md for the remaining agenda.
'''

GOAL_PROMPT_ONLY = '''Prompt-only adapter. The whole served context is ADAPTER.md (sections: # Adapter context,
## Procedure, ## Conventions, ## Lessons, ## Open questions). Read it with read_file(area="adapter",
path="ADAPTER.md") and rewrite it whole with write_file. A lesson is a "### L-<slug>" heading under
## Lessons followed by the key: value lines and a short body. Keep every section; keep the
six self-checks in ## Procedure; put conventions you verified under ## Conventions with the
lesson id in brackets. adapter.json and EVOLVER.md must not change (the candidate is rejected
if they do). Bytes are reported, not cut; write for a reader who acts under time pressure.
'''

PROMPT_ONLY_OPENER = '''You evolve a prompt-only Harness Adapter: one learned-context document, ADAPTER.md, served
whole to the next acting agent. Read adapter/EVOLVER.md, then follow the update goal below
phase by phase. Only ADAPTER.md changes in the adapter; scratch/ takes your records. Report what
you verified, not what you believe. Return a candidate; do not activate it.'''

PHASE_TEMPLATE = '''PHASE: episode {ordinal:03} ({task_id}). strict {strict}, 1% {one_percent}. status {status}, failure_kind {failure_kind}, terminal_event {terminal_event}, error_text {error_text}.
Question: {question}
Submitted: {submitted}
Reference: {reference}
Read evidence/{ordinal:03}/trajectory.jsonl and failure.json. Name the first failed rung (S1-S7). If no completed
turn produced an answer, the rung is S7: read the failure record, write the harness cause, leave the ladder open.
Otherwise verify each rung with verify_cell / run_command / verify_answer(ordinal={ordinal}, candidate=...) until
strict==1 or the chain is open at the last passing rung. Write scratch/postmortem/{ordinal:03}.json and one lesson
per re-verified rung with its check list. Then stop with no tool call.'''

SERVE_PHASE = '''SERVING PHASE. Every lesson you wrote this session: confirm it has status, scope, step, check,
verified_by and brief lines and that each check names a released ordinal or a real corpus line
(run verify_cell / verify_answer once more if unsure). Remove duplicates; merge lessons that share a
family. MWM: refresh consumption/brief.md so it routes to the lessons index. Prompt-only: reread
ADAPTER.md and make sure it is complete and ordered. Write scratch/feedback.json =
{"label":"glm_reference_answer_feedback","episodes":[{"id":...,"observations":...,
"reference_comparison":...,"uncertainty":...,"proposed_lesson":...} x5]}. Call validate. Then
stop with no tool call: a short public summary of records, lessons and open chains.'''

CONSULT_RULE = '''Before opening a table, match its family here; before committing a number, match the convention here.
State units, count cells, round last; commit once a candidate number exists; mention the [id] you apply.'''

TOOLS = {
    'verify_answer': {'type': 'object', 'additionalProperties': False, 'required': ['ordinal', 'candidate'],
                      'properties': {'ordinal': {'type': 'integer', 'minimum': 1}, 'candidate': {'type': 'string'}}},
    'verify_cell': {'type': 'object', 'additionalProperties': False, 'required': ['file', 'line', 'contains'],
                    'properties': {'file': {'type': 'string'}, 'line': {'type': 'integer', 'minimum': 1},
                                   'contains': {'type': 'string'}}}}
DESCRIPTIONS = {
    'verify_answer': 'Grade a candidate answer against the released reference of one ordinal of this block (strict and 1%). Other ordinals are refused.',
    'verify_cell': 'Read one corpus TXT line and report whether it contains the text (whitespace/comma tolerant), with two neighbouring lines each side.'}
DECLARATIONS = [{'type': 'function', 'function': {'name': n, 'description': DESCRIPTIONS[n], 'parameters': TOOLS[n]}}
                for n in sorted(TOOLS)]
FAILURE_KEYS = ('status', 'failure_kind', 'terminal_event', 'error_text', 'requests', 'wire_attempts',
                'call_failures', 'elapsed_seconds', 'agents_md_bytes', 'project_doc_max_bytes')
LESSON_KEYS = ('status', 'scope', 'step', 'check', 'brief', 'verified_by')
# Where learners have put session records (the same drift the feedback file shows).
RECORD_DIRS = ('scratch/postmortem', 'scratch/scratch/postmortem', 'adapter/scratch/postmortem')


def _json(path):
    path = Path(path)
    return json.loads(path.read_text()) if path.is_file() else {}


def grader(reward_path):
    score = load_grader(reward_path)
    return score, score.__globals__['normalize_text']


def answer_verdict(reference, candidate, score):
    return {'strict': int(score(reference, candidate, tolerance=0.0)), 'one_percent': int(score(reference, candidate, tolerance=0.01))}


def grade_block(evidence_dir, out_dir, block, reward_path):
    evidence_dir, out_dir = Path(evidence_dir), Path(out_dir)
    score, _ = grader(reward_path)
    manifest = _json(evidence_dir / 'manifest.json')
    episodes = []
    for episode in sorted(manifest.get('episodes', []), key=lambda e: e['ordinal']):
        base = evidence_dir / episode['directory']
        task, prediction, reference = (_json(base / n) for n in ('task.json', 'prediction.json', 'reference_answer.json'))
        summary, failure = _json(base / 'summary.json'), _json(base / 'failure.json')
        record = {'ordinal': episode['ordinal'], 'task_id': episode['id'], 'question': task.get('question'),
                  'submitted': prediction.get('answer') or '', 'reference': reference.get('official_answer') or ''}
        record.update(answer_verdict(record['reference'], record['submitted'], score))
        for key in FAILURE_KEYS:
            record[key] = failure.get(key, summary.get(key))
        record['wm_calls'] = (summary.get('native_codex_items') or {}).get('mcp_tool_call')
        episodes.append(record)
        _save(out_dir / f'{episode["ordinal"]:03}.json', record)
    verdicts = {'block': block, 'grader_sha256': sha256(Path(reward_path)), 'episodes': episodes}
    _save(out_dir / 'verdicts.json', verdicts)
    return verdicts


def _save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def panel(verdicts, previous=None):
    lines = [f'## Block {verdicts["block"]} verdicts (released for this block only)']
    for e in verdicts['episodes']:
        reason = e.get('terminal_event') or e.get('status') or 'unknown'
        if e.get('error_text'): reason += f' ({e["error_text"]})'
        lines += [f'### {e["ordinal"]:03} {e["task_id"]}', f'Question: {e["question"]}',
                  f'Submitted: {e["submitted"] or "(empty)"}', f'Reference: {e["reference"]}',
                  f'Verdict: strict {e["strict"]}, 1% {e["one_percent"]}. Termination: {reason}; failure_kind {e.get("failure_kind")}; '
                  f'requests {e.get("requests")}; wire attempts {e.get("wire_attempts")}; call failures {e.get("call_failures")}; '
                  f'AGENTS.md bytes {e.get("agents_md_bytes")} / limit {e.get("project_doc_max_bytes")}; wm calls {e.get("wm_calls")}.']
    n = len(verdicts['episodes'])
    lines.append(f'Summary: strict {sum(e["strict"] for e in verdicts["episodes"])}/{n}, 1% {sum(e["one_percent"] for e in verdicts["episodes"])}/{n}.')
    if previous:
        lines.append(f'Previous replay: {previous.get("verified", 0)} lessons verified, {previous.get("demoted", 0)} demoted, '
                     f'{previous.get("promoted", 0)} promoted, {previous.get("wrong", 0)} with failing checks, {previous.get("records", 0)} records.')
        demoted = [l['id'] for l in previous.get('lessons', []) if l.get('status_before') == 'verified' and not l.get('passed')]
        if demoted: lines.append('Demoted: ' + ', '.join(demoted))
    return '\n'.join(lines) + '\n'


def phases(verdicts):
    return [PHASE_TEMPLATE.format(**{k: e.get(k) for k in ('ordinal', 'task_id', 'strict', 'one_percent', 'status', 'failure_kind',
            'terminal_event', 'error_text', 'question', 'submitted', 'reference')})
            for e in sorted(verdicts['episodes'], key=lambda e: e['ordinal']) if not e['strict']]


def goal_text(architecture):
    return GOAL_COMMON + (GOAL_MWM if architecture == 'mwm' else GOAL_PROMPT_ONLY)


def _plain(text, normalize):
    return normalize(text).replace(',', '').replace('$', '')


def cell_check(docs, file, line, contains, normalize):
    start = max(1, line - 2)
    rows = {}
    for row in docs.read(file, start_line=start, num_lines=line + 3 - start)['content'].split('\n'):
        m = re.match(r'(\d+): (.*)', row)
        if m: rows[int(m.group(1))] = m.group(2)
    if line not in rows: raise ValueError('Line is beyond the end of the file')
    text = rows[line]
    found = bool(text) and (normalize(contains) in normalize(text) or _plain(contains, normalize) in _plain(text, normalize))
    return {'file': file, 'line': line, 'found': found, 'line_text': text, 'neighbours': [rows[k] for k in sorted(rows)]}


def call(name, args, ctx):
    try:
        score, normalize = grader(ctx.get('reward', VENDOR / 'reward.py'))
        if name == 'verify_answer':
            episode = next((e for e in ctx['manifest']['episodes'] if e['ordinal'] == args['ordinal']), None)
            if episode is None: raise ValueError('Ordinal is not in this released block')
            reference = _json(Path(ctx['evidence']) / episode['directory'] / 'reference_answer.json').get('official_answer') or ''
            result = {'ordinal': args['ordinal'], **answer_verdict(reference, args['candidate'], score),
                      'normalized_candidate': normalize(args['candidate']), 'normalized_reference': normalize(reference)}
        elif name == 'verify_cell':
            result = cell_check(OfficeQACorpus(ctx['corpus'], ctx['index']), args['file'], args['line'], args['contains'], normalize)
        else: raise ValueError('Unknown checker')
        return json.dumps(result, ensure_ascii=False), True
    except Exception as exc:
        return json.dumps({'error': type(exc).__name__ + ': ' + str(exc)}), False


def _header(lines):
    fields = {}
    for line in lines:
        m = re.match(r'(status|scope|step|check|brief|verified_by):\s*(.*)$', line)
        if m and m.group(1) not in fields: fields[m.group(1)] = m.group(2).strip()
    try: check = json.loads(fields.get('check') or '[]')
    except ValueError: check = []
    return {k: fields.get(k) for k in LESSON_KEYS if k != 'check'} | {'check': check if isinstance(check, list) else []}


def parse_lessons(candidate, architecture):
    candidate = Path(candidate)
    lessons = []
    if architecture == 'mwm':
        for path in sorted((candidate / 'memory').glob('L-*.md')):
            lines = path.read_text().splitlines()
            lessons.append({'id': path.stem, 'path': str(path), 'span': (0, len(lines)), **_header(lines)})
        return lessons
    path = candidate / 'ADAPTER.md'
    if not path.is_file(): return lessons
    lines = path.read_text().splitlines()
    heads = [i for i, l in enumerate(lines) if re.match(r'#{1,3} ', l)]
    for i in heads:
        m = re.match(r'### (L-[\w-]+)\s*$', lines[i])
        if not m: continue
        end = next((j for j in heads if j > i), len(lines))
        lessons.append({'id': m.group(1), 'path': str(path), 'span': (i, end), **_header(lines[i + 1:end])})
    return lessons


def write_status(candidate, architecture, lesson, new_status):
    """Exactly one status line per lesson, the controller's; any other status line in the block goes."""
    path = Path(lesson['path'])
    lines = path.read_text().splitlines()
    start, end = lesson['span']
    found = [i for i in range(start, end) if re.match(r'status:', lines[i])]
    for i in reversed(found[1:]): del lines[i]
    if found: lines[found[0]] = f'status: {new_status}'
    else: lines.insert(start + (architecture != 'mwm'), f'status: {new_status}')
    path.write_text('\n'.join(lines) + '\n')


def replay(candidate, architecture, block, evidence_dirs, corpus, index, reward_path):
    candidate = Path(candidate)
    score, normalize = grader(reward_path)
    docs = OfficeQACorpus(corpus, index)
    lessons, pending = [], []
    for lesson in parse_lessons(candidate, architecture):
        checks = []
        for spec in lesson['check']:
            try:
                if not isinstance(spec, dict): raise ValueError('Check must be an object')
                if spec.get('kind') == 'answer':
                    ordinal = int(spec['ordinal'])
                    if ordinal not in evidence_dirs: raise ValueError('Reference not released')
                    reference = _json(Path(evidence_dirs[ordinal]) / 'reference_answer.json').get('official_answer') or ''
                    verdict = answer_verdict(reference, str(spec['candidate']), score)
                    checks.append({'spec': spec, 'passed': verdict['strict'] == 1, 'detail': verdict})
                elif spec.get('kind') == 'cell':
                    result = cell_check(docs, spec['file'], int(spec['line']), str(spec['contains']), normalize)
                    checks.append({'spec': spec, 'passed': result['found'], 'detail': result['line_text']})
                else: raise ValueError('Unsupported check kind (only cell and answer replay)')
            except Exception as exc:
                checks.append({'spec': spec, 'passed': False, 'detail': type(exc).__name__ + ': ' + str(exc)})
        passed = bool(checks) and all(c['passed'] for c in checks)
        before, after = lesson['status'], 'verified' if passed else 'candidate'
        pending.append((lesson, after))
        lessons.append({'id': lesson['id'], 'status_before': before, 'status_after': after, 'checks': checks, 'passed': passed})
    # Always write, last block first, so an inserted line never shifts a span still to be written.
    for lesson, after in sorted(pending, key=lambda x: x[0]['span'][0], reverse=True):
        write_status(candidate, architecture, lesson, after)
    session = candidate.parent
    summary = {'block': block, 'architecture': architecture, 'lessons': lessons,
               'verified': sum(l['passed'] for l in lessons), 'candidate': sum(not l['passed'] for l in lessons),
               'demoted': sum(l['status_before'] == 'verified' and not l['passed'] for l in lessons),
               'promoted': sum(l['status_before'] != 'verified' and l['passed'] for l in lessons),
               'wrong': sum(bool(l['checks']) and not l['passed'] for l in lessons),
               'records': sum(len(list((session / p).glob('*.json'))) for p in RECORD_DIRS)}
    _save(session / 'control/postmortem-replay.json', summary)
    return summary


def served_bytes(candidate, architecture):
    candidate = Path(candidate)
    name = 'consumption/brief.md' if architecture == 'mwm' else 'ADAPTER.md'
    key = 'brief' if architecture == 'mwm' else 'adapter_md'
    text = (candidate / name).read_text() if (candidate / name).is_file() else ''
    lessons = parse_lessons(candidate, architecture)
    return {key: len(text), 'lessons': len(lessons), 'verified_lessons': sum(l['status'] == 'verified' for l in lessons)}
