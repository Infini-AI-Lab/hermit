"""Scripted toy episodes: synthetic Codex Responses items driven through Ledger and Gate exactly in
the order the broker sees them (request N -> ingest_request, response N -> ingest_response, the tool
runs, a `submit` POST reaches the gate, the tool output appears in request N+1).

Tool outputs carry the real Codex header ('Chunk ID', 'Wall time', exit code, token count), calc
outputs are real calc frames (the code is executed by harness_core.tools.calc with `python`), and
`submit` calls Gate.submit. Each item has a script with a typical weak-model trajectory; five of
them contain the mistake a rule targets (first printing, percent for a decimal, missing value,
wrong quarter). With `gate=None` the first submission is the final answer ("no-gate" episodes for
replay statistics); with a gate, the scripted model accepts offers (or keeps, per script).
"""
from __future__ import annotations

import hashlib
import json
import shlex
import sys
from typing import Callable

from harness_core import contracts as C
from harness_core.tools import calc as calc_tool

from .items import ITEMS
from .tools import lookup_text

TOOLS = [{'type': 'function', 'name': n} for n in ('exec_command', 'write_stdin')]


def codex_output(text: str, rc: int = 0, n: int = 0) -> str:
    chunk = hashlib.sha256(str(n).encode()).hexdigest()[:6]
    return (f'Chunk ID: {chunk}\nWall time: {0.01 * (n % 7):.4f} seconds\nProcess exited with code {rc}\n'
            f'Original token count: {max(1, len(text) // 4)}\nOutput:\n{text}')


class Sim:
    def __init__(self, question: str, ledger, gate=None, *, python: str | None = None, accept_offers: bool = True):
        self.ledger, self.gate, self.python, self.accept_offers = ledger, gate, python or sys.executable, accept_offers
        self.items: list[dict] = [
            {'type': 'message', 'id': 'msg_dev', 'role': 'developer', 'content': [{'type': 'input_text', 'text': 'dev'}]},
            {'type': 'message', 'id': 'msg_q', 'role': 'user', 'content': [{'type': 'input_text', 'text': question}]}]
        self.call, self.t = 0, 0.0
        self.results: list[C.GateResult] = []
        self.final_text = ''

    def _request(self) -> None:
        self.call += 1
        self.t += 1.0
        self.ledger.ingest_request(self.call, list(self.items), self.t, tools=TOOLS)

    def run(self, cmd: str, execute: Callable[[], tuple[str, int]]) -> str:
        self._request()
        call_id = f'call_{self.call}'
        fc = {'type': 'function_call', 'id': f'fc_{self.call}', 'call_id': call_id, 'name': 'exec_command',
              'arguments': json.dumps({'cmd': cmd, 'workdir': '/ws', 'yield_time_ms': 10000})}
        self.ledger.ingest_response(self.call, {'status': 'completed', 'output': [fc]}, self.t)
        self.items.append(fc)
        text, rc = execute()
        self.items.append({'type': 'function_call_output', 'id': f'fco_{self.call}', 'call_id': call_id,
                           'output': codex_output(text, rc, self.call)})
        return text

    def lookup(self, file: str, *rows: str, table: str | None = None) -> str:
        cmd = 'lookup ' + ' '.join(shlex.quote(x) for x in (file, *rows)) + (f' --table {table}' if table else '')
        return self.run(cmd, lambda: (lookup_text(file, list(rows), table), 0))

    def calc(self, code: str) -> str:
        def execute() -> tuple[str, int]:
            norm = calc_tool.normalise(code)
            rc, out, err = calc_tool.execute(norm, python=self.python, timeout=30)
            return calc_tool.frame(norm, out) + (('\n' + err.strip()) if rc else ''), rc
        return self.run('calc -c ' + shlex.quote(code), execute)

    def submit(self, answer: str, *, alt: str | None = None, keep: str | None = None) -> C.GateResult | None:
        payload = {'answer': answer, **({'alt': alt} if alt else {}), **({'keep': keep} if keep else {})}
        cmd = 'submit ' + ' '.join(f'--{k} {shlex.quote(v)}' for k, v in payload.items())

        def execute() -> tuple[str, int]:
            if self.gate is None:
                return 'submit: no submit route in this environment (HARNESS_SUBMIT_URL is not set)', 2
            res = self.gate.submit(payload, self.ledger, self.t, self.call)
            self.results.append(res)
            return res.message, 0
        self.run(cmd, execute)
        return self.results[-1] if self.gate is not None else None

    def submit_loop(self, answer: str, *, alt: str | None = None, accept: bool = True,
                    keep_reason: str = 'checked against the source', fix: dict | None = None) -> str:
        """Submit, then react to findings the way the script says: accept offers, apply a fix, or keep."""
        res = self.submit(answer, alt=alt)
        for _ in range(4):
            if res is None or res.status != 'findings' or not res.findings:
                break
            f = res.findings[0]
            if f.offer and accept and self.accept_offers:
                answer = f.offer.answer
                res = self.submit(answer)
            elif fix and f.rule in fix:
                answer = fix[f.rule]
                res = self.submit(answer)
            else:
                res = self.submit(answer, keep=keep_reason)
        return self.final(answer)

    def final(self, text: str) -> str:
        self._request()                        # carries the last tool output
        msg = {'type': 'message', 'id': f'msg_{self.call}', 'role': 'assistant',
               'content': [{'type': 'output_text', 'text': text}]}
        self.ledger.ingest_response(self.call, {'status': 'completed', 'output': [msg]}, self.t)
        self.final_text = text
        return text


def _s01(s: Sim) -> str:                     # first printing used (later_printing)
    s.lookup('widgets_2020_a.txt', 'North')
    s.calc('print(round(120.5 + 130.25, 2))')
    return s.submit_loop('250.75')


def _s02(s: Sim) -> str:                     # right; --alt from the first printing (competing, kept)
    s.lookup('widgets_2020_b.txt', 'South')
    s.calc('print((110.3 - 98.9) / 98.9 * 100)')
    return s.submit_loop('11.53', alt='12.09', accept=False, keep_reason='the revised printing is the latest')


def _s03(s: Sim) -> str:                     # percent given for a decimal (scale)
    s.lookup('widgets_2021_a.txt', 'East')
    s.calc('r = (101.0 - 88.0) / 88.0\nprint(r)\nprint(r * 100)')
    return s.submit_loop('14.77')


def _s04(s: Sim) -> str:                     # right, silent
    s.lookup('widgets_2020_b.txt', 'East')
    s.calc('print((75 + 80.5 + 82.25 + 91.5) / 4)')
    return s.submit_loop('82.3')


def _s05(s: Sim) -> str:                     # one value for two (answer_count; fixed by the model)
    s.lookup('widgets_2020_a.txt', 'West')
    s.lookup('widgets_2021_a.txt', 'West')
    return s.submit_loop('65.7', fix={'answer_count': '65.7, 71.9'})


def _s06(s: Sim) -> str:                     # right; question pins the first printing
    s.lookup('widgets_2020_a.txt', 'North', 'South')
    s.calc('print(120.5 / 98.4)')
    return s.submit_loop('1.225')


def _s07(s: Sim) -> str:                     # first-printing total (later_printing)
    s.lookup('widgets_2020_a.txt', 'Total')
    s.lookup('widgets_2021_a.txt', 'Total')
    s.calc('print(round(410.6 - 374.25, 2))')
    return s.submit_loop('36.35')


def _s08(s: Sim) -> str:                     # right; plain look-up
    s.lookup('widgets_2021_a.txt', table='W-2')
    return s.submit_loop('2.80')


def _s09(s: Sim) -> str:                     # Q2 price used for Q1 (period_match)
    s.lookup('widgets_2021_a.txt', 'East', 'Price')
    s.calc('print(round(88.0 * 2.75))')
    return s.submit_loop('242')


def _s10(s: Sim) -> str:                     # right, typed without calc (recompute_final fires; kept)
    s.lookup('widgets_2021_a.txt', 'West')
    return s.submit_loop('0.1340', accept=False, keep_reason='(75.3 - 66.4) / 66.4 by hand')


SCRIPTS: dict[str, Callable[[Sim], str]] = {
    'toy01': _s01, 'toy02': _s02, 'toy03': _s03, 'toy04': _s04, 'toy05': _s05,
    'toy06': _s06, 'toy07': _s07, 'toy08': _s08, 'toy09': _s09, 'toy10': _s10,
}
QUESTIONS = {it.item_id: it.question for it in ITEMS}


def run_item(item_id: str, ledger, gate=None, **kwargs) -> Sim:
    sim = Sim(QUESTIONS[item_id], ledger, gate, **kwargs)
    SCRIPTS[item_id](sim)
    return sim
