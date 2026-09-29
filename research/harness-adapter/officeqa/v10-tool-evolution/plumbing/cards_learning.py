"""Cards evolver host for the OfficeQA campaign.

Per learning block: grade the sealed batch with the pinned official grader,
run one GLM learner session that may propose at most B_MAX cards, verify each
proposal by re-running the actor with the proposal added (on the questions it
names as support and on control questions the controller picks), and assemble
the next deck from the admitted cards only.

The learner writes nothing. Its only channel to the deck is propose_card, and a
proposal reaches the deck only after block.judge admits its paired re-runs.
Everything the controller does is recorded under evolution/NN/.

Pairing: support and control questions are drawn only from questions whose
original scored attempt used the current deck (state['deck_since']), so the
re-run with the proposal is compared with an attempt that differed by the
proposal alone. Older wrong questions stay visible to the learner as evidence.
"""
import ast
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import math
import operator
import os
from pathlib import Path
import threading
import time

import campaign as c
import acting
import broker
import client
import experiment
import postmortem
import learning as legacy
from learning import save, utc, snapshot, revision, digest
from paths import REPOSITORY, VENDOR

ROOT = c.ROOT
MODEL = client.MODEL
EVOLVER = REPOSITORY / 'harnessadapter/evolvers/cards'
GENERIC_SOURCE = c.W0
CONCURRENCY = 8            # verification actors running at once
LEARNER_CALL_CAP = 400     # a runaway learner ends its session; recorded, never expected
LEARNER_CALL_SECONDS = 3600  # total bound of one learner request (a dead socket is detected, generation is not cut short)
CHECKPOINT_TOKENS = 600000 # rolling context: the learner summarises itself and continues
PAGE = 20000               # characters per evidence page
PROPOSAL_ATTEMPTS = 20     # screened proposals a session may make before propose_card refuses
EARLIER_REPORT_CHARS = 12000
# Questions the benchmark's own issue tracker lists as questionable (databricks/officeqa#47:
# 15 answer updates + 3 clarification examples; v7 listed only 3 of the 18): scored like
# every other question, but never used as support or control for a card.
EXCLUDED_TASKS = ('UID0001', 'UID0004', 'UID0007', 'UID0049', 'UID0056', 'UID0065', 'UID0093', 'UID0124',
                  'UID0140', 'UID0154', 'UID0155', 'UID0179', 'UID0190', 'UID0194', 'UID0238',
                  'UID0175', 'UID0213', 'UID0237')
STATE = ROOT / 'cards-state.json'
LEDGER = ROOT / 'cards-ledger.jsonl'

_spec = importlib.util.spec_from_file_location('cards_block', EVOLVER / 'block.py')
block = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(block)
schema, render = block.core()

CHECKPOINT_PROMPT = ('Write a checkpoint for your own continuation: evidence inspected, causes found (facts versus '
                     'hypotheses), verifications done, cards proposed and their ids, open work and next actions. '
                     'Do not call tools in this checkpoint. The original assignment and all tools remain available.')
NUDGE = (f'You ended without proposing any card. If one rule would have changed the outcome of '
         f'{"one" if block.SUPPORT_MIN == 1 else block.SUPPORT_MIN} or more pool questions (name only questions the rule '
         f'really decides), propose it now with propose_card; otherwise reply "no card" and explain why in one paragraph.')
SUPPORT_TEXT = (f'support: {"one" if block.SUPPORT_MIN == 1 else block.SUPPORT_MIN} or more pool ordinals (q numbers) whose '
                f'outcome this exact rule would have changed; each is re-run {block.K} times and misses allowed = (support count - 1), '
                f'so name only questions the rule really decides.')

TOOL_DECLARATIONS = [
    {'name': 'read_evidence', 'description': 'Read one file of a released episode. name: task, prediction, reference (with the strict and 1% verdicts), failure, summary, trajectory (compact: the actor\'s messages, commands and outputs with long ones cut in the middle, paged) or trajectory_full (raw JSON lines, paged). Long files are paged by offset; the reply gives next_offset, and offset = total_chars - 20000 reads the final page.',
     'parameters': {'type': 'object', 'additionalProperties': False, 'required': ['ordinal', 'name'],
                    'properties': {'ordinal': {'type': 'integer', 'minimum': 1}, 'name': {'type': 'string', 'enum': ['task', 'prediction', 'reference', 'failure', 'summary', 'trajectory', 'trajectory_full']}, 'offset': {'type': 'integer', 'minimum': 0}}}},
    {'name': 'search_corpus', 'description': 'Full-text search over the corpus the actor used (OR of the query words, BM25 ranked, excerpts with file and line anchors).',
     'parameters': {'type': 'object', 'additionalProperties': False, 'required': ['query'],
                    'properties': {'query': {'type': 'string'}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 20}, 'filename_glob': {'type': 'string'}}}},
    {'name': 'read_corpus', 'description': 'Read numbered lines of one corpus TXT file (1-150 lines).',
     'parameters': {'type': 'object', 'additionalProperties': False, 'required': ['filename'],
                    'properties': {'filename': {'type': 'string'}, 'start_line': {'type': 'integer', 'minimum': 1}, 'num_lines': {'type': 'integer', 'minimum': 1, 'maximum': 150}}}},
    {'name': 'list_corpus', 'description': 'List corpus file names matching a glob pattern.',
     'parameters': {'type': 'object', 'additionalProperties': False,
                    'properties': {'pattern': {'type': 'string'}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}, 'offset': {'type': 'integer', 'minimum': 0}}}},
    {'name': 'verify_cell', 'description': postmortem.DESCRIPTIONS['verify_cell'], 'parameters': postmortem.TOOLS['verify_cell']},
    {'name': 'verify_answer', 'description': 'Grade a candidate answer against the released reference of any released ordinal with the official grader (strict and 1%).', 'parameters': postmortem.TOOLS['verify_answer']},
    {'name': 'calc', 'description': 'Evaluate one arithmetic expression at full precision: + - * / ** % //, parentheses, lists, abs round min max sum len sqrt log exp mean sorted, and the constants pi and e.',
     'parameters': {'type': 'object', 'additionalProperties': False, 'required': ['expression'], 'properties': {'expression': {'type': 'string', 'maxLength': 2000}}}},
    {'name': 'propose_card', 'description': 'Propose one card for verification. scope: 1-8 comma-separated keywords or short phrases taken from the wording of the questions the rule applies to (whole words; every admitted card is shown on every task, scope only picks control questions and records later matches). claim: the rule, at most 600 bytes, plain lines. unless: when not to apply it, at most 300 bytes (may be empty). ' + SUPPORT_TEXT + ' The screen answers at once with reasons; an accepted card is re-run after the session. After B accepted proposals further ones are refused.',
     'parameters': {'type': 'object', 'additionalProperties': False, 'required': ['scope', 'claim', 'support', 'rationale'],
                    'properties': {'scope': {'type': 'string'}, 'claim': {'type': 'string'}, 'unless': {'type': 'string'},
                                   'support': {'type': 'array', 'items': {'type': 'integer', 'minimum': 1}, 'minItems': 1},
                                   'rationale': {'type': 'string', 'description': 'For the ledger: the first failed step of each support question and why this rule fixes it.'}}}},
]
FUNCTIONS = [{'type': 'function', 'function': t} for t in TOOL_DECLARATIONS]
SCHEMAS = {t['name']: t['parameters'] for t in TOOL_DECLARATIONS}


def load_state():
    if STATE.is_file():
        return json.loads(STATE.read_text())
    return {'claimed': {}, 'rejected_sha': [], 'hits': {}, 'next_id': 1, 'deck_since': 1}


def save_state(state):
    temporary = STATE.with_name(STATE.name + '.tmp')
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n')
    os.replace(temporary, STATE)


def load_ledger():
    return [json.loads(l) for l in LEDGER.read_text().splitlines() if l.strip()] if LEDGER.is_file() else []


def append_ledger(rows):
    with LEDGER.open('a') as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')


def released_records(block_no):
    """Every question whose reference has been released: blocks 1..block_no, graded now."""
    score, _ = postmortem.grader(VENDOR / 'reward.py')
    records = {}
    for bb in range(1, block_no + 1):
        base = ROOT / 'evidence' / f'{bb:02}'
        manifest = json.loads((base / 'manifest.json').read_text())
        for episode in manifest['episodes']:
            d = base / episode['directory']
            task = json.loads((d / 'task.json').read_text())
            prediction = json.loads((d / 'prediction.json').read_text())
            reference = json.loads((d / 'reference_answer.json').read_text())['official_answer']
            summary = json.loads((d / 'summary.json').read_text())
            failure = json.loads((d / 'failure.json').read_text())
            usage = summary.get('usage') or {}
            tokens = usage.get('output_tokens')
            records[episode['ordinal']] = {
                'ordinal': episode['ordinal'], 'task_id': episode['id'], 'question': task['question'],
                'submitted': prediction['answer'], 'reference': reference,
                'strict': int(score(reference, prediction['answer'], tolerance=0.0)),
                'one_percent': int(score(reference, prediction['answer'], tolerance=0.01)),
                'status': failure.get('status'), 'transport_failed': bool(summary.get('transport_failed')),
                'out_tokens': tokens if isinstance(tokens, int) and not isinstance(tokens, bool) else None,
                'block': bb, 'dir': str(d)}
    return records


def _cut(text, head, tail, limit):
    text = str(text or '')
    if len(text) <= limit:
        return text
    return text[:head] + f' ... [{len(text) - head - tail} chars omitted] ... ' + text[-tail:]


def compact_trajectory(path):
    out = []
    corpus, index = str(c.CORPUS), str(c.INDEX)
    for number, line in enumerate(Path(path).read_text().splitlines(), 1):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        item = event.get('item') or {}
        kind = item.get('type') or event.get('type')
        if kind == 'agent_message':
            out.append(f'[{number}] MESSAGE: {item.get("text", "")}')
        elif kind == 'command_execution':
            command = str(item.get('command', '')).replace(index, '<INDEX>').replace(corpus, '<CORPUS>')
            out.append(f'[{number}] COMMAND (exit {item.get("exit_code")}): {_cut(command, 600, 1400, 2000)}\n'
                       f'    OUTPUT: {_cut(item.get("aggregated_output"), 1500, 500, 2000)}')
        elif kind == 'mcp_tool_call':
            out.append(f'[{number}] TOOL: {json.dumps(item, ensure_ascii=False)[:1500]}')
        elif kind == 'file_change':
            out.append(f'[{number}] FILE_CHANGE: {json.dumps(item, ensure_ascii=False)[:500]}')
        elif event.get('type') in ('turn.completed', 'turn.failed'):
            out.append(f'[{number}] {event["type"].upper()}: {json.dumps(event.get("usage") or event.get("error") or {})[:300]}')
    return '\n'.join(out)


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.Mod: operator.mod, ast.FloorDiv: operator.floordiv, ast.USub: operator.neg, ast.UAdd: operator.pos}
_FUNCS = {'abs': abs, 'round': round, 'min': min, 'max': max, 'sum': sum, 'len': len, 'sqrt': math.sqrt, 'log': math.log,
          'exp': math.exp, 'mean': lambda xs: sum(xs) / len(xs), 'sorted': sorted, 'float': float, 'int': int}


def calc(expression):
    """Arithmetic only: no names, attributes, comprehensions or calls outside the whitelist."""
    tree = ast.parse(expression, mode='eval')

    def evaluate(node):
        if isinstance(node, ast.Expression):
            return evaluate(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            left, right = evaluate(node.left), evaluate(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 10000:
                raise ValueError('exponent too large')
            return _OPS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](evaluate(node.operand))
        if isinstance(node, (ast.List, ast.Tuple)):
            return [evaluate(x) for x in node.elts]
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS and not node.keywords:
            return _FUNCS[node.func.id](*[evaluate(a) for a in node.args])
        if isinstance(node, ast.Name) and node.id in ('pi', 'e'):
            return getattr(math, node.id)
        raise ValueError(f'unsupported expression element: {type(node).__name__}')
    value = evaluate(tree)
    return {'expression': expression, 'value': value if isinstance(value, (int, float)) else str(value), 'repr': repr(value)}


def dispatch(name, args, ctx, state):
    released = ctx['released']
    if name == 'read_evidence':
        record = released.get(args['ordinal'])
        if record is None:
            raise ValueError('That ordinal has no released reference; only released episodes can be read')
        d, which, offset = Path(record['dir']), args['name'], args.get('offset', 0)
        if which == 'trajectory':
            text = compact_trajectory(d / 'trajectory.jsonl')
        elif which == 'trajectory_full':
            text = (d / 'trajectory.jsonl').read_text()
        elif which == 'reference':
            text = json.dumps({'ordinal': record['ordinal'], 'official_answer': record['reference'],
                               'strict': record['strict'], 'one_percent': record['one_percent']})
        else:
            text = (d / f'{which}.json').read_text()
        page = text[offset:offset + PAGE]
        return json.dumps({'ordinal': args['ordinal'], 'name': which, 'offset': offset, 'total_chars': len(text),
                           'next_offset': offset + PAGE if offset + PAGE < len(text) else None, 'content': page}, ensure_ascii=False), True
    if name in ('search_corpus', 'read_corpus', 'list_corpus'):
        docs = ctx['docs']
        function = {'search_corpus': docs.search, 'read_corpus': docs.read, 'list_corpus': docs.list_files}[name]
        return json.dumps(function(**args), ensure_ascii=False), True
    if name == 'verify_cell':
        return json.dumps(postmortem.cell_check(ctx['docs'], args['file'], args['line'], args['contains'], ctx['normalize']), ensure_ascii=False), True
    if name == 'verify_answer':
        record = released.get(args['ordinal'])
        if record is None:
            raise ValueError('That ordinal has no released reference')
        verdict = postmortem.answer_verdict(record['reference'], args['candidate'], ctx['score'])
        return json.dumps({'ordinal': args['ordinal'], **verdict, 'normalized_candidate': ctx['normalize'](args['candidate']),
                           'normalized_reference': ctx['normalize'](record['reference'])}, ensure_ascii=False), True
    if name == 'calc':
        return json.dumps(calc(args['expression'])), True
    if name == 'propose_card':
        return propose(args, ctx, state)
    raise ValueError('Unknown tool')


def propose(args, ctx, state):
    ctx['proposal_attempts'] += 1
    if len(state['proposals']) >= block.B_MAX:
        return json.dumps({'accepted': False, 'id': None, 'reasons': [f'at most {block.B_MAX} accepted proposals per block; this one is not recorded']}), False
    if ctx['proposal_attempts'] > PROPOSAL_ATTEMPTS:
        return json.dumps({'accepted': False, 'id': None, 'reasons': ['proposal attempts for this session are exhausted']}), False
    card = {'id': f"C{ctx['next_id']:03}", 'scope': args['scope'], 'claim': args['claim'],
            'unless': args.get('unless', '') or '', 'support': list(args['support'])}
    existing = ctx['deck_ids'] + [p['id'] for p in state['proposals']]
    verdict = block.screen(card, ctx['pool'], ctx['released'], ctx['state']['rejected_sha'], existing)
    reasons = list(verdict['reasons'])
    if not reasons:
        # Room check: the deck's own byte budget, with every proposal already accepted this block counted.
        pending = sum(len(('\n' + schema.render(p)).encode()) for p in state['proposals'])
        needed = ctx['layout']['bytes'] + pending + len(('\n' + schema.render(card)).encode())
        if ctx['layout']['overflow'] or needed > render.CARDS_MAX:
            reasons.append(f'deck full: the compiled deck would exceed {render.CARDS_MAX} bytes')
    ctx['screen_log'].append({'t': ctx['block'], 'attempt': ctx['proposal_attempts'], 'card': card, 'rationale': args.get('rationale', ''),
                              'sha': verdict['sha'], 'ok': not reasons, 'reasons': reasons, 'utc': utc()})
    if reasons:
        return json.dumps({'accepted': False, 'id': None, 'reasons': reasons}), False
    ctx['next_id'] += 1
    state['proposals'].append({**card, 'sha': verdict['sha'], 'rationale': args.get('rationale', '')})
    return json.dumps({'accepted': True, 'id': card['id'],
                       'note': 'Accepted for verification. After this session the actor is re-run with this card on the support questions and on control questions; only then can it enter the deck.'}), True


def learner_session(key, session, prompt, ctx, stop):
    """One GLM conversation with controller-implemented tools; every call is recorded."""
    from jsonschema import Draft202012Validator
    control = session / 'control'
    control.mkdir(parents=True, exist_ok=True)
    save(control / 'fireworks-tools.json', FUNCTIONS)
    (control / 'prompt.md').write_text(prompt)
    messages = [{'role': 'user', 'content': prompt}]
    state = {'status': 'running', 'model_calls': 0, 'requested_tools': 0, 'successful_tools': 0,
             'usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'cached_tokens': 0},
             'usage_complete': True, 'usage_unknown_calls': [], 'wire_attempts': 0, 'rate_refusals': 0,
             'response_models': [], 'compactions': 0, 'last_prompt_tokens': 0, 'proposals': [], 'nudges': 0, 'runaways': 0}

    def checkpoint():
        save(control / 'fireworks-result.json', state)

    def call(history, purpose, **options):
        state['model_calls'] += 1
        number = state['model_calls']
        state['in_flight_request'] = number
        checkpoint()
        response, meta = client.complete(key, history, timeout=LEARNER_CALL_SECONDS, unbounded=True, **options)
        state.pop('in_flight_request', None)
        usage = meta.get('usage', {})
        for field in ('prompt_tokens', 'completion_tokens'):
            if legacy.token_count(usage.get(field)):
                state['usage'][field] += usage[field]
        cached = (usage.get('prompt_tokens_details') or {}).get('cached_tokens', 0)
        if legacy.token_count(cached):
            state['usage']['cached_tokens'] += cached
        if meta.get('usage_complete') is False:
            state['usage_complete'] = False
            state['usage_unknown_calls'].append(number)
        state['wire_attempts'] += meta.get('wire_attempts', 1)
        state['rate_refusals'] += sum(a.get('status') == 'rate_refused' for a in meta.get('attempts', []))
        public_history = [client.public_message(m) if m.get('role') == 'assistant' else m for m in history]
        choice = (response.get('choices') or [{}])[0] if response else {}
        message = choice.get('message') or {}
        record = {'request': number, 'purpose': purpose, 'requested_model': MODEL, 'meta': meta,
                  'public_request_sha256': revision(public_history), 'public_message_count': len(history),
                  'request_max_tokens': None, 'reasoning_effort': None,
                  'message': client.public_message(message), 'finish_reason': choice.get('finish_reason')}
        save(control / f'fireworks-calls/{number:03}.json', record)
        checkpoint()
        if response is None:
            raise RuntimeError('GLM transport failed; see recorded provider metadata')
        actual = meta.get('response_model') or response.get('model')
        if actual != MODEL or message.get('role') != 'assistant' or meta.get('http_status') != 200:
            raise ValueError('Unexpected model or response')
        if actual not in state['response_models']:
            state['response_models'].append(actual)
        state['last_prompt_tokens'] = usage.get('prompt_tokens', 0)
        return message, choice.get('finish_reason')

    while True:
        if stop.is_set():
            state['status'] = 'aborted'
            break
        if state['model_calls'] >= LEARNER_CALL_CAP:
            state['status'] = 'learner_call_cap'
            break
        if state['last_prompt_tokens'] >= CHECKPOINT_TOKENS:
            summary, finish = call(messages + [{'role': 'user', 'content': CHECKPOINT_PROMPT}], 'glm_context_checkpoint',
                                   tools=FUNCTIONS, tool_choice='none')
            text = summary.get('content') or ''
            if not text.strip() or finish == 'length':
                state['status'] = 'checkpoint_failed'
                break
            state['compactions'] += 1
            (control / f'glm-checkpoint-{state["compactions"]:02}.md').write_text(text)
            resume = ('Continue the same learning session from your own checkpoint. Re-read evidence whenever needed. '
                      'Proposals already accepted for verification: ' + json.dumps([p['id'] for p in state['proposals']]))
            messages = [{'role': 'user', 'content': prompt}, {'role': 'assistant', 'content': text}, {'role': 'user', 'content': resume}]
            state['last_prompt_tokens'] = 0
        message, finish = call(messages, 'learner', tools=FUNCTIONS)
        calls = message.get('tool_calls') or []
        if finish == 'length' and not calls and not str(message.get('content') or '').strip() and state['runaways'] < 3:
            # An output-cap runaway with nothing visible: re-send the same history rather
            # than carry ~65K reasoning tokens forward (at most 3 times per session).
            state['runaways'] += 1
            continue
        messages.append(client.continuing_message(message))
        if not calls:
            if finish == 'length':
                messages.append({'role': 'user', 'content': 'The provider ended that response at its output limit. Continue the same session; tools are unchanged.'})
                continue
            if finish == 'stop' and not state['proposals'] and len(ctx['pool']) >= block.SUPPORT_MIN and state['nudges'] < 1:
                state['nudges'] += 1
                messages.append({'role': 'user', 'content': NUDGE})
                continue
            state['status'] = 'completed' if finish == 'stop' else 'provider_incomplete'
            (control / 'fireworks-report.md').write_text(message.get('content') or '')
            break
        for tool_call in calls:
            state['requested_tools'] += 1
            index = state['requested_tools']
            function = tool_call.get('function', {})
            name, args = function.get('name'), None
            record = {'index': index, 'request': state['model_calls'], 'name': name, 'tool_call_id': tool_call['id'], 'started_utc': utc()}
            try:
                if name not in SCHEMAS:
                    raise ValueError('Unknown tool')
                args = json.loads(function.get('arguments') or '{}')
                Draft202012Validator(SCHEMAS[name]).validate(args)
                text, ok = dispatch(name, args, ctx, state)
            except Exception as exc:
                text, ok = json.dumps({'error': (type(exc).__name__ + ': ' + str(exc)).replace(key, '[REDACTED]')}), False
            state['successful_tools'] += int(ok)
            record.update(arguments=args, ok=ok, response=text if len(text) <= 200000 else text[:200000] + '...')
            save(control / f'fireworks-tool-results/{index:03}.json', record)
            messages.append({'role': 'tool', 'tool_call_id': tool_call['id'], 'content': text})
        checkpoint()
    checkpoint()
    return state


def verify_actor(key, base, task, adapter):
    """One fresh actor attempt for verification; same runner, sandbox and wall clock as the scored runs."""
    base = Path(base)
    workspace, records = base / 'workspace', base / 'records'
    if base.exists():
        raise ValueError('A verification attempt is made once; prior directory exists')
    base.mkdir(parents=True)
    c.prepare_workspace(workspace, adapter)
    save(base / 'task.json', task)
    with broker.Broker(key, base / 'api', **c.BROKER_OPTIONS) as relay:
        result = acting.run_actor(workspace, records, c.prompt_for(task), c.CORPUS, c.INDEX,
                                  adapter=adapter, timeout=c.ACTOR_SECONDS, endpoint=relay.endpoint)
    transport = json.loads((base / 'api/summary.json').read_text())
    final = records / 'final.md'
    answer = final.read_text().strip() if result.get('status') == 'completed' and final.is_file() else ''
    if result.get('timed_out'):
        # The actor's own timeout, even with a request still streaming at the kill.
        status, transport_failed = 'timeout', False
    else:
        status = ((transport.get('failure_kind') or 'transport_rejected') if transport.get('failed') else result.get('status'))
        transport_failed = bool(transport.get('transport_failed'))
    usage = transport.get('usage') or {}
    tokens = usage.get('output_tokens')
    summary = {'task_id': task['task_id'], 'status': status, 'answer': answer, 'elapsed_seconds': result.get('elapsed_seconds'),
               'requests': transport.get('calls'), 'usage': usage, 'transport_failed': transport_failed,
               'call_failures': transport.get('call_failures', 0), 'terminal_event': result.get('terminal_event'),
               'scheduler_wait_seconds': transport.get('scheduler_wait_seconds'), 'rate_refusals': transport.get('rate_refusals'),
               'agents_md_bytes': result.get('agents_md_bytes'),
               'out_tokens': tokens if isinstance(tokens, int) and not isinstance(tokens, bool) else None}
    save(base / 'summary.json', summary)
    return summary


def validate_source(source, block_no):
    """The source deck is the generic architecture or this campaign's previous admitted candidate."""
    before = snapshot(source)
    if Path(source) == GENERIC_SOURCE.resolve():
        return before, {'kind': 'generic_cards_architecture', 'revision': revision(before)}
    receipt = Path(source).parent / 'result.json'
    prior = json.loads(receipt.read_text())
    if (block_no <= 1 or prior.get('profile') != experiment.PROFILE or not prior.get('activated')
            or prior.get('candidate') != str(source) or prior.get('revision') != revision(before)
            or not 0 < prior.get('block', 0) < block_no or prior.get('model') != MODEL):
        raise ValueError("Source must be this campaign's previous admitted candidate")
    return before, {'kind': 'previous_admitted_candidate', 'receipt_sha256': digest(receipt.read_bytes())}


def earlier_report(block_no):
    path = ROOT / 'evolution' / f'{block_no - 1:02}' / 'sessions' / 'learner' / 'control' / 'fireworks-report.md'
    if block_no <= 1 or not path.is_file():
        return ''
    text = path.read_text().strip()
    if not text:
        return ''
    return (f'\n## Earlier learner report (block {block_no - 1}; unverified notes, reuse the leads and re-verify)\n'
            + text[-EARLIER_REPORT_CHARS:] + '\n')


def run_learning(key, source, evidence, output, block_no, stop=None):
    stop = stop or threading.Event()
    source, evidence, output = (Path(p).resolve() for p in (source, evidence, output))
    if output.exists():
        raise FileExistsError('Learning trial already exists')
    output.mkdir(parents=True)
    start = time.monotonic()
    state_saved = False
    result = {'status': 'retained', 'activated': False, 'block': block_no, 'model': MODEL, 'source': str(source),
              'candidate': str(source), 'started_utc': utc(), 'profile': experiment.PROFILE,
              'architecture': experiment.ARCHITECTURE, 'admission_mode': 'paired_actor_rerun', 'transport': 'fireworks'}
    session = output / 'sessions' / 'learner'
    ledger_rows, state = [], None
    try:
        before, lineage = validate_source(source, block_no)
        legacy.validate_evidence(evidence, block_no)
        manifest = json.loads((evidence / 'manifest.json').read_text())
        seal = legacy.validate_batch_seal(evidence, manifest, block_no)
        score, normalize = postmortem.grader(VENDOR / 'reward.py')
        verdicts = postmortem.grade_block(evidence, output / 'postmortem', block_no, VENDOR / 'reward.py')
        released = released_records(block_no)
        excluded = [o for o, r in released.items() if r['task_id'] in EXCLUDED_TASKS]
        state = load_state()
        ledger = load_ledger()
        deck = render.load(source)
        layout = render.layout(source)
        # Prequential record: this block's questions were answered with the source deck.
        for card in deck:
            if card['id'] not in layout['rendered']:
                continue
            for e in manifest['episodes']:
                r = released[e['ordinal']]
                if schema.matches(r['question'], card['scope']):
                    state.setdefault('hits', {}).setdefault(card['id'], []).append([e['ordinal'], r['strict']])
        # Hits are facts about the sealed block: keep them even if this learning round fails.
        save_state(state)
        # Pairing: only questions answered under the current deck can be support or control.
        since = int(state.get('deck_since', 1))
        current = {o: r for o, r in released.items() if r['block'] >= since}
        pool = block.pool(current, state['claimed'], excluded)
        right_ids = block.right(current, excluded)
        stale = [o for o in block.pool(released, state['claimed'], excluded) if o not in pool]
        state['next_id'] = block.next_id(state.get('next_id', 1), deck, ledger)
        domain = (ROOT / 'cards_domain.md').read_text() if (ROOT / 'cards_domain.md').is_file() else ''
        prompt = ((EVOLVER / 'PROMPT.md').read_text() + '\n' + domain + '\n' + postmortem.panel(verdicts) + '\n'
                  + block.status_text(block_no, pool, right_ids, released, deck, state.get('hits', {}), ledger, layout, stale)
                  + ('Excluded from support and control (questionable references): ' + json.dumps(excluded) + '\n' if excluded else '')
                  + earlier_report(block_no))
        from officeqa_tools import OfficeQACorpus
        ctx = {'released': released, 'pool': pool, 'right': right_ids, 'block': block_no, 'state': state, 'layout': layout,
               'deck_ids': [c_['id'] for c_ in deck], 'next_id': state['next_id'], 'screen_log': [],
               'proposal_attempts': 0, 'docs': OfficeQACorpus(c.CORPUS, c.INDEX), 'score': score, 'normalize': normalize}
        save(output / 'request.json', {'block': block_no, 'source_lineage': lineage, 'episode_ids': [e['id'] for e in manifest['episodes']],
                                       'pool': pool, 'right': right_ids, 'stale': stale, 'excluded': excluded, 'deck_since': since,
                                       'controller_batch_seal': seal, 'domain_sha256': digest(domain.encode()),
                                       'budgets': {'seconds': None, 'completion_tokens': None, 'model_calls': LEARNER_CALL_CAP,
                                                   'seconds_per_call': LEARNER_CALL_SECONDS},
                                       'b_max': block.B_MAX, 'k': block.K, 'keep': block.KEEP, 'cost_max': block.COST_MAX})
        learner = learner_session(key, session, prompt, ctx, stop)
        for name in ('model_calls', 'requested_tools', 'successful_tools', 'usage', 'usage_complete', 'usage_unknown_calls',
                     'wire_attempts', 'rate_refusals', 'response_models', 'compactions'):
            result[name] = learner[name]
        result['learner_status'] = learner['status']
        save(output / 'screen.json', ctx['screen_log'])
        proposals = learner['proposals']
        result['proposals'] = [{k: p[k] for k in ('id', 'scope', 'claim', 'unless', 'support', 'sha')} for p in proposals]
        if learner['response_models'] not in ([], [MODEL]):
            raise ValueError('Model provenance failed')
        # Verification: every accepted proposal, all runs at once.
        jobs, trials, keeps, unrendered = [], {}, {}, []
        for card in proposals:
            trial = output / 'verify' / card['id'] / 'adapter'
            block.assemble(source, trial, [card], block_no)
            if card['id'] not in render.layout(trial)['rendered']:
                unrendered.append(card['id'])
                continue
            trials[card['id']] = trial
            support = schema.support_list(card['support'])
            keeps[card['id']] = block.choose_keep(right_ids, card['scope'], released, exclude=support)
            for q in support:
                jobs += [(card['id'], q, 'support', j) for j in range(1, block.K + 1)]
            for q in keeps[card['id']]:
                jobs += [(card['id'], q, 'keep', j) for j in range(1, block.K + 1)]
        runs = {card['id']: [] for card in proposals}
        lock = threading.Lock()

        def attempt(card_id, q, role, j, suffix):
            base = output / 'verify' / card_id / f'q{q:03}-k{j}{suffix}'
            record = released[q]
            task = {'task_id': record['task_id'], 'question': record['question']}
            try:
                summary = verify_actor(key, base, task, trials[card_id])
                strict = int(score(record['reference'], summary['answer'], tolerance=0.0)) if summary['answer'] else 0
                row = {'q': q, 'role': role, 'k': j, 'strict': strict, 'status': summary['status'],
                       'out_tokens': summary['out_tokens'], 'excluded': bool(summary['transport_failed']),
                       'elapsed_seconds': summary['elapsed_seconds'], 'requests': summary['requests'],
                       'scheduler_wait_seconds': summary.get('scheduler_wait_seconds'), 'dir': base.name}
            except Exception as exc:
                row = {'q': q, 'role': role, 'k': j, 'strict': 0, 'status': 'launch_error', 'out_tokens': None,
                       'excluded': True, 'dir': base.name, 'error': (type(exc).__name__ + ': ' + str(exc)).replace(key, '[REDACTED]')}
            save(base / 'verdict.json', row)
            return row

        def run_job(job):
            card_id, q, role, j = job
            if stop.is_set():
                row = {'q': q, 'role': role, 'k': j, 'strict': 0, 'status': 'aborted', 'out_tokens': None, 'excluded': True}
            else:
                row = attempt(card_id, q, role, j, '')
                if row['excluded'] and not stop.is_set():
                    # One fresh attempt restores the evidence a lost transport took away.
                    retry = attempt(card_id, q, role, j, '-r1')
                    retry['retried'] = row
                    row = retry
            with lock:
                runs[card_id].append(row)
                save(output / 'verification.json', {'jobs': len(jobs), 'done': sum(len(v) for v in runs.values()), 'runs': runs})
            print(json.dumps({'learning_block': block_no, 'verify': card_id, 'q': q, 'role': role, 'k': j,
                              'status': row['status'], 'strict': row['strict'], 'excluded': row['excluded']}), flush=True)
            return row
        save(output / 'verification.json', {'jobs': len(jobs), 'done': 0, 'runs': runs})
        if jobs:
            with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool_:
                list(pool_.map(run_job, jobs))
        originals = {o: r['out_tokens'] for o, r in released.items()}
        admitted, rejected_sha = [], []
        for card in proposals:
            public = {k: card[k] for k in ('id', 'scope', 'claim', 'unless', 'support')}
            if card['id'] in unrendered:
                ledger_rows.append({'t': block_no, 'id': card['id'], 'action': 'deck_full', 'claim_sha': card['sha'], 'card': public,
                                    'admitted': False, 'reason': 'not rendered within the deck byte budget', 'rationale': card['rationale'], 'utc': utc()})
                continue
            verdict = block.judge(sorted(runs[card['id']], key=lambda r: (r['role'], r['q'], r['k'])), originals)
            ledger_rows.append({'t': block_no, 'id': card['id'], 'action': 'add', 'claim_sha': card['sha'], 'card': public,
                                'scope': schema.keywords(card['scope']), 'support': schema.support_list(card['support']),
                                'keep': keeps[card['id']], 'runs': runs[card['id']], 'tokens_ratio': verdict['tokens_ratio'],
                                'admitted': verdict['admitted'], 'inconclusive': verdict['inconclusive'], 'reason': verdict['reason'],
                                'rationale': card['rationale'], 'session': str(session), 'utc': utc()})
            if verdict['admitted']:
                admitted.append(card)
            elif not verdict['inconclusive']:
                rejected_sha.append(card['sha'])
        for row in ctx['screen_log']:
            if not row['ok']:
                ledger_rows.append({'t': block_no, 'id': None, 'action': 'screen_rejected', 'claim_sha': row['sha'], 'card': row['card'],
                                    'admitted': False, 'reason': '; '.join(row['reasons']), 'rationale': row['rationale'], 'utc': utc()})
        result['admitted'] = [card['id'] for card in admitted]
        result['verification_episodes'] = len(jobs)
        result['verification_summary'] = {card['id']: block.judge(runs[card['id']], originals)['reason'] for card in proposals if card['id'] in trials}
        candidate = source
        if admitted:
            candidate = output / 'adapter'
            from harnessadapter.adapter.versioning import commit_all, ensure_repo
            # History starts at the untouched copy of the source deck; each block is one commit.
            written = block.assemble(source, candidate, admitted, block_no, after_copy=ensure_repo)
            commit = commit_all(candidate, f'cards: block {block_no} admits {", ".join(card["id"] for card in admitted)}')
            after = snapshot(candidate)
            # The framework's first commit adds .gitignore; the learner never wrote anything.
            added = {k for k in after if k not in before} - {'.gitignore'}
            changed = {k for k in before if k in after and after[k] != before[k]}
            if changed or added != {f'cards/{name}' for name in written}:
                raise ValueError('Assembled deck differs from the source by more than the admitted cards')
            result.update(status='admitted', activated=True, candidate=str(candidate), candidate_commit=commit,
                          revision=revision(after), added=sorted(added))
        else:
            result['revision'] = revision(before)
        if snapshot(source) != before:
            raise ValueError('Source deck changed during learning')
        # Durable records are written only now, when the block's outcome is final.
        for card in admitted:
            for q in schema.support_list(card['support']):
                state['claimed'][str(q)] = card['id']
        state['rejected_sha'] = list(dict.fromkeys(state.get('rejected_sha', []) + rejected_sha))
        state['next_id'] = ctx['next_id']
        if admitted:
            state['deck_since'] = block_no + 1
        final_layout = render.layout(candidate)
        state['rendered'], state['overflow'] = final_layout['rendered'], final_layout['overflow']
        append_ledger(ledger_rows)
        ledger_rows = []
        save_state(state)
        state_saved = True
        save(session / 'control' / 'mechanical-admission.json', {
            'admitted': bool(admitted), 'admitted_cards': [card['id'] for card in admitted],
            'operation_provenance_matches': True, 'human_semantic_gate': False,
            'protected_unchanged': {'source': True, 'evidence': True},
            'revision': result.get('revision'), 'note': 'Admission is the paired actor re-run verdict recorded in cards-ledger.jsonl; the learner wrote no file.'})
    except Exception as exc:
        result['error'] = (type(exc).__name__ + ': ' + str(exc)).replace(key, '[REDACTED]')
        result.setdefault('learner_status', 'failed')
        if not state_saved:
            # The claims and deck_since of a new deck were never recorded: keep serving the source deck.
            result.update(status='failed', activated=False, candidate=str(source))
        if ledger_rows:
            # Outcome rows exist only in memory; record them as void so nothing reads them as active.
            append_ledger([{**row, 'action': 'void', 'admitted': False, 'void_reason': result['error']} for row in ledger_rows])
        partial = session / 'control' / 'fireworks-result.json'
        if partial.is_file():
            recorded = json.loads(partial.read_text())
            for name in ('model_calls', 'requested_tools', 'usage', 'usage_complete', 'usage_unknown_calls', 'wire_attempts', 'rate_refusals'):
                result[name] = recorded.get(name)
    result['session'] = str(session)
    result.update(finished_utc=utc(), elapsed_seconds=round(time.monotonic() - start, 3))
    save(output / 'result.json', result)
    return result
