"""Replay candidate checkers over all 399 v8 arm-runs and write results.jsonl.

Usage: python3.11 run_replay.py            # compute results.jsonl
       python3.11 report.py                # render ../replay.md from results.jsonl

results.jsonl holds flags, counts and value-free reasons only (no answer values).
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import checkers as C  # noqa: E402
import load  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def main() -> None:
    t0 = time.time()
    runs = load.load_runs()
    ann = load.per_question_annotations()
    out = open(os.path.join(HERE, 'results.jsonl'), 'w')
    for k, r in enumerate(runs):
        fmt = C.format_check(r.question, r.answer)
        corpus = C.Corpus.from_steps(r.steps)
        visible = C.Corpus.from_texts(load.visible_outputs(r.run_dir)) if r.answer.strip() else None
        prov = C.provenance_check(r.steps, r.answer, r.question, corpus=corpus, visible=visible)
        loop = C.loop_detect(r.steps)
        doubt = C.doubt_unresolved(r.steps)
        doubt['fired_w5'] = C.doubt_unresolved(r.steps, window=5)['fired']
        doubt['fired_w20'] = C.doubt_unresolved(r.steps, window=20)['fired']
        rec = {
            'arm': r.arm, 'ordinal': r.ordinal, 'task_id': r.task_id, 'correct': r.correct,
            'status': r.status, 'empty': not r.answer.strip(), 'error_bin': r.error_bin,
            'annotation': ann.get((r.arm, r.ordinal)),
            'format': fmt, 'format_fired': C.format_fired(fmt),
            'format_fired_soft': C.format_fired(fmt, include_soft=True),
            'provenance': prov, 'loop': loop, 'doubt': doubt,
            'n_reason': sum(1 for s in r.steps if s.kind == 'reason'),
        }
        out.write(json.dumps(rec) + '\n')
        if k % 50 == 0:
            print(f'{k}/{len(runs)} {time.time() - t0:.0f}s', flush=True)
    out.close()
    print(f'done {time.time() - t0:.0f}s')


if __name__ == '__main__':
    main()
