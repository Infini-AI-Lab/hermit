"""Render ../replay.md from results.jsonl (value-free). Run run_replay.py first."""
from __future__ import annotations

import collections
import glob
import json
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import checkers as C  # noqa: E402
import load  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.path.dirname(HERE), 'replay.md')
ARMS = ['none', 'prompt', 'cards']
COSMETIC = {'STRAY_TEXT', 'STRAY_UNIT'}
STRICT_FLAGS = [f for f in C.HARD_FORMAT_FLAGS if f not in COSMETIC]

R = [json.loads(line) for line in open(os.path.join(HERE, 'results.jsonl'))]
SUB = [r for r in R if not r['empty']]


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (float('nan'), float('nan'))
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (c - h, c + h)


def m(recs, pred):
    w = [r for r in recs if not r['correct']]
    c = [r for r in recs if r['correct']]
    fw = sum(1 for r in w if pred(r))
    fc = sum(1 for r in c if pred(r))
    n = fw + fc
    prec = fw / n if n else float('nan')
    base = len(w) / len(recs) if recs else float('nan')
    return {'nw': len(w), 'nc': len(c), 'fw': fw, 'fc': fc, 'prec': prec, 'ci': wilson(fw, n),
            'rec': fw / len(w) if w else float('nan'), 'frc': fc / len(c) if c else float('nan'),
            'base': base, 'lift': prec / base if n and base else float('nan')}


def pct(x):
    return '–' if x != x else f'{100 * x:.0f}%'


def lift_str(x):
    return '–' if x != x else f'{x:.2f}'


def fmt_row(name, arm, s):
    lo, hi = s['ci']
    ci = '' if s['fw'] + s['fc'] == 0 else f' [{100 * lo:.0f}–{100 * hi:.0f}]'
    return (f"| {name} | {arm} | {s['fw']}/{s['nw']} ({pct(s['rec'])}) | {s['fc']}/{s['nc']} ({pct(s['frc'])}) | "
            f"**{pct(s['prec'])}**{ci} | {pct(s['rec'])} | {pct(s['base'])} | "
            f"{lift_str(s['lift'])} |")


HEAD = ('| checker | arm | fired on wrong (= recall) | fired on right | precision P(wrong\\|fired) [95% CI] | recall P(fired\\|wrong) | base P(wrong) | lift |\n'
        '|---|---|---|---|---|---|---|---|')


def table(checks, pop):
    lines = [HEAD]
    for name, pred in checks:
        for arm in ARMS + ['ALL']:
            recs = [r for r in pop if arm == 'ALL' or r['arm'] == arm]
            lines.append(fmt_row(name if arm == 'none' else '', arm, m(recs, pred)))
    return '\n'.join(lines)


def compact_table(rows, pop, label='variant'):
    """rows: (name, pred). One line per variant with per-arm P/R and pooled P/R."""
    head = f'| {label} | ' + ' | '.join(f'{a}: fired w/r · P · R' for a in ARMS) + ' | ALL: fired w/r · P [CI] · R |'
    lines = [head, '|' + '---|' * (len(ARMS) + 2)]
    for name, pred in rows:
        cells = []
        for arm in ARMS + ['ALL']:
            s = m([r for r in pop if arm == 'ALL' or r['arm'] == arm], pred)
            ci = f" [{100 * s['ci'][0]:.0f}–{100 * s['ci'][1]:.0f}]" if arm == 'ALL' and s['fw'] + s['fc'] else ''
            cells.append(f"{s['fw']}/{s['fc']} · {pct(s['prec'])}{ci} · {pct(s['rec'])}")
        lines.append(f'| {name} | ' + ' | '.join(cells) + ' |')
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
fmt_a = lambda r: r['format_fired']  # noqa: E731
fmt_s = lambda r: any(k in r['format']['flags'] for k in STRICT_FLAGS)  # noqa: E731
prov = lambda r: r['provenance']['fired']  # noqa: E731
loop = lambda r: r['loop']['fired']  # noqa: E731
doubt = lambda r: r['doubt']['fired']  # noqa: E731
union = lambda r: fmt_a(r) or prov(r) or loop(r)  # noqa: E731
union_s = lambda r: fmt_s(r) or prov(r) or loop(r)  # noqa: E731

MAIN = [('(a) format_check', fmt_a), ('(a\\*) format_check, cosmetic flags dropped', fmt_s),
        ('(b) provenance_check', prov), ('(c) loop_detect', loop), ('(d) doubt_unresolved', doubt),
        ('(a)+(b)+(c) union', union), ('(a\\*)+(b)+(c) union', union_s)]


def fp_examples(pred, reason, arm, k=5):
    rows = [r for r in R if r['arm'] == arm and r['correct'] and pred(r)]
    rows.sort(key=lambda r: r['ordinal'])
    return [f'Q{r["ordinal"]:03}: {reason(r)}' for r in rows[:k]], len(rows)


def reason_format(r):
    return '; '.join(f'{k} — {v}' for k, v in r['format']['flags'].items() if k in C.HARD_FORMAT_FLAGS)


def reason_prov(r):
    return ' | '.join(r['provenance']['reasons'][:2])


def reason_loop(r):
    L = r['loop']
    return (f"one command issued {L['max_count']}x; 3rd issue at command {L['first_third_at']} of {L['n_cmds']}; "
            f"longest identical streak {L['max_consecutive']}; {L['compactions']} compaction(s)"
            f"{', at least one after the loop fired' if L['compacted_after_loop'] else ', none after the loop'}; answered correctly")


def reason_doubt(r):
    d = r['doubt']
    return (f"{d['doubt_notes']} of the last 10 reasoning notes contain doubt words ({', '.join(d['words'])}); "
            f"the last one was followed by 0 commands — the model answered straight after, and was right")


def loop_mechanism():
    """On runs with an identical-command streak: how often the preceding
    reasoning note is identical too, and what ends the longest streak."""
    runs = load.load_runs()
    n_runs = pairs = same = at_comp = at_end = other = 0
    last_doubt = with_reason = 0
    for r in runs:
        rs = [s for s in r.steps if s.kind == 'reason']
        if rs:
            with_reason += 1
            last_doubt += bool(C.DOUBT.search(rs[-1].text))
        L = C.loop_detect(r.steps)
        if not L['fired_consecutive']:
            continue
        n_runs += 1
        prev_reason = prev_cmd = prev_pair = None
        seq = []  # (kind, normalised command)
        for st in r.steps:
            if st.kind == 'reason':
                prev_reason = st.text
            elif st.kind == 'cmd':
                c = ' '.join(st.text.split())
                if c == prev_cmd:
                    pairs += 1
                    same += (prev_reason, c) == prev_pair
                prev_cmd, prev_pair = c, (prev_reason, c)
                seq.append(('cmd', c))
            elif st.kind == 'compaction':
                seq.append(('compaction', ''))
        cmd_pos = [i for i, (k, _) in enumerate(seq) if k == 'cmd']
        best_len, best_end, cur = 0, 0, 1
        for j in range(1, len(cmd_pos) + 1):
            if j < len(cmd_pos) and seq[cmd_pos[j]][1] == seq[cmd_pos[j - 1]][1]:
                cur += 1
                continue
            if cur > best_len:
                best_len, best_end = cur, cmd_pos[j - 1]
            cur = 1
        nxt = seq[best_end + 1][0] if best_end + 1 < len(seq) else 'end'
        at_comp += nxt == 'compaction'
        at_end += nxt == 'end'
        other += nxt == 'cmd'
    return {'runs': n_runs, 'pairs': pairs, 'same_reason': same, 'ends_at_compaction': at_comp,
            'ends_at_end': at_end, 'ends_other': other, 'last_note_doubt': last_doubt, 'with_reason': with_reason}


def compaction_marker_check():
    """Compaction 'error' items in events vs tool-less (compaction) API requests."""
    agree = differ = 0
    detail = []
    for r in R:
        if r['loop']['compactions'] == 0:
            continue
        d = load.run_dir(r['arm'], r['ordinal'])
        n = 0
        for path in glob.glob(f'{d}/api/[0-9]*/request.json'):
            with open(path, 'rb') as fh:
                fh.seek(max(0, os.path.getsize(path) - 200_000))
                tail = fh.read()
            k = tail.rfind(b'"tools":')
            if k < 0 or tail[k + 8:k + 12].lstrip().startswith(b'[]'):
                n += 1
        if n == r['loop']['compactions']:
            agree += 1
        else:
            differ += 1
            detail.append(f"{r['arm']} Q{r['ordinal']:03} ({r['loop']['compactions']} markers vs {n} tool-less requests)")
    return agree, differ, detail


def main():
    n_all = len(R)
    lab = collections.Counter((r['arm'], r['correct']) for r in R)
    empties = collections.Counter(r['arm'] for r in R if r['empty'])
    agree, differ, cdetail = compaction_marker_check()
    mech = loop_mechanism()
    L = []
    w = L.append

    # ---------- numbers used in the text
    s_loop = m(R, loop)
    s_union = m(R, union)
    s_union_s = m(R, union_s)
    s_prov = m(SUB, prov)
    s_doubt = m(R, doubt)
    s_fmt = m(R, fmt_a)
    s_fmt_sub = m(SUB, fmt_a)
    s_fmts_sub = m(SUB, fmt_s)
    s_fmts = m(R, fmt_s)
    n_cmds = np.array([r['loop']['n_cmds'] for r in R])
    q75 = float(np.quantile(n_cmds, 0.75))
    top = [r for r in R if r['loop']['n_cmds'] > q75]
    top_loop, top_noloop = m([r for r in top if loop(r)], lambda r: True), m([r for r in top if not loop(r)], lambda r: True)
    wasted_all = sum(r['loop']['wasted_all'] for r in R)
    loop_cmds = sum(r['loop']['n_cmds'] for r in R if r['loop']['fired'])
    total_cmds = int(n_cmds.sum())
    fired_loop = [r for r in R if loop(r)]
    frac = np.median([r['loop']['first_third_at'] / r['loop']['n_cmds'] for r in fired_loop])
    med_at = np.median([r['loop']['first_third_at'] for r in fired_loop])
    after_comp = sum(1 for r in fired_loop if r['loop']['compacted_after_loop'])

    w('# v9 understand: offline replay of candidate commit-gate checkers over the v8 runs')
    w('')
    w('Date: 2026-09-28. Zero-cost, deterministic replay of four candidate checkers over all '
      f'{n_all} v8 arm-runs (3 roots x 133 questions). Labels: `officeqa-divergence-analysis/index.json` only. '
      'This report contains no answer values (question ordinals, flags, counts and relative distances only).')
    w('')
    w('## TL;DR')
    w('')
    w(f"- **loop_detect is the only checker that clearly separates wrong from right runs**: precision "
      f"{pct(s_loop['prec'])} (95% CI {100 * s_loop['ci'][0]:.0f}–{100 * s_loop['ci'][1]:.0f}) vs a base rate of {pct(s_loop['base'])}, "
      f"recall {pct(s_loop['rec'])} ({s_loop['fw']} of {s_loop['nw']} wrong runs; {s_loop['fc']} right runs also fire). "
      "What it detects is a temperature-0 repetition collapse (same reasoning + same command + same output, dozens to hundreds of times) "
      f"that typically ends only when Codex auto-compacts the context ({after_comp}/{len(fired_loop)} firing runs compact after the firing point). It is, however, mostly a proxy for "
      f"\"this run is long\": within the top command-count quartile, looping runs are wrong {pct(top_loop['nw'] / max(1, top_loop['nw'] + top_loop['nc']))} "
      f"vs {pct(top_noloop['nw'] / max(1, top_noloop['nw'] + top_noloop['nc']))} for non-looping ones. Its value for v9 is as an early *control* trigger "
      f"(median first firing at command {med_at:.0f}, i.e. {100 * frac:.0f}% into the run), not as an answer verdict.")
    w(f"- **format_check has almost nothing to catch.** GLM followed the asked rounding in every run where the decimals check applied. "
      "Stray units/words/`$` fire as often on right runs as on wrong ones (the grader evidently extracts the number), so they are cosmetic here. "
      f"Without them the only flags that fire on wrong runs are EMPTY (timeouts, {sum(empties.values())} runs) and COUNT (2 multi-number answers): "
      f"precision {pct(s_fmts['prec'])} but recall {pct(s_fmts['rec'])} over all runs. As specified (with the cosmetic flags) it is at the base rate "
      f"on submitted answers: precision {pct(s_fmt_sub['prec'])}, recall {pct(s_fmt_sub['rec'])}.")
    w(f"- **provenance_check does not separate**: precision {pct(s_prov['prec'])} vs base {pct(s_prov['base'])} on submitted answers "
      f"(recall {pct(s_prov['rec'])}). Right answers are often typed from 2–3-number mental arithmetic or a hand unit conversion; wrong "
      "answers mostly use numbers that *did* appear in outputs (wrong cell / wrong vintage), which a provenance check cannot see. "
      "No variant (model-visible outputs only, rescaling-tolerant, echo-aware, calc-output-only) moves precision off the base rate.")
    w(f"- **doubt_unresolved is noise**: almost every run (right or wrong) has doubt words in its last 10 reasoning notes, and the "
      f"\"no command after the last doubting note\" rule has precision {pct(s_doubt['prec'])} vs base {pct(s_doubt['base'])}. "
      "A keyword-addressed variant is more precise but catches only a handful of runs.")
    w(f"- **Union (a)+(b)+(c)**: precision {pct(s_union['prec'])}, recall {pct(s_union['rec'])} ({s_union['fw']} wrong / {s_union['fc']} right fire); "
      f"with cosmetic format flags dropped: precision {pct(s_union_s['prec'])}, recall {pct(s_union_s['rec'])}. "
      "Almost all of the union's lift comes from loop_detect and empty answers.")
    w(f"- **Implication for v9**: a commit gate built from these three checks would bounce about as many right answers as wrong ones "
      "(except for loops and empties), so L4 needs *content* checkers (period/label match, vintage conflict, parts-sum-to-total) that "
      "these trajectories cannot score without the ledger. The replay argues for spending L3 on the loop guard (inject a perturbing "
      f"nudge at the 3rd identical command+output; {wasted_all:,} of {total_cmds:,} v8 commands ({100 * wasted_all / total_cmds:.0f}%) were the 3rd-or-later "
      "issue of an identical command with an identical output) and submit-best-candidate at timeout, and against bouncing answers for units/`%`/commas.")
    w('')

    w('## 1. Data and method')
    w('')
    w(f"- Runs: {n_all} = 3 roots (`officeqa-pro133-{{none,prompt,cards}}-v8-work/runs/{{baseline,adapted,adapted}}/NNN`) x 133. "
      "Question from each run's `task.json`; submitted answer from `summary.json` `answer` "
      "(= stripped `final.md` for completed runs, empty otherwise: `officeqa-pro133-v8-plumbing/campaign.py:134`).")
    w('- Labels per arm (index.json `correct`): ' + ', '.join(
        f"{a} {lab[(a, 1)]} right / {lab[(a, 0)]} wrong" for a in ARMS) + '. Empty answers (all timeouts, all wrong): ' +
      ', '.join(f'{a} {empties[a]}' for a in ARMS) + '.')
    w('- Two populations: **all runs** (399) and **submitted answers** (non-empty, '
      f'{len(SUB)}), the population a commit gate would actually see.')
    w('- Trajectory: `records/events.jsonl`. Commands are taken in issue order from `item.started` and joined to '
      '`item.completed` for `aggregated_output` (`replay/load.py:57`). `aggregated_output` is the *full* output; the model saw a '
      'Codex-truncated copy (~10 KB, head+tail with a "…N chars truncated…" marker). The model-visible copy is reconstructed '
      'from the `function_call_output` items of the compaction requests plus the final request (`replay/load.py:127`).')
    w(f"- Compaction marker: Codex writes an `error` item \"Heads up: Long threads and multiple compactions …\" after each auto-compaction "
      f"(auto-compact limit 160,000 tokens, `officeqa-pro133-v8-plumbing/acting.py:284`). Verified against the API log: in {agree} of "
      f"{agree + differ} runs with markers the marker count equals the number of tool-less (`\"tools\": []`) compaction requests"
      + (f"; mismatches: {', '.join(cdetail)} (a compaction still in flight at the wall-clock kill)." if cdetail else '.'))
    w('- The broker forces `temperature=0` on every request (`officeqa-pro133-v8-plumbing/broker.py:90`); this matters for (c).')
    w('')
    w('Checker definitions (code in `officeqa-v9-design/understand/replay/`):')
    w('')
    w('| checker | fires when | code |')
    w('|---|---|---|')
    w('| (a) format_check | question-spec parser (count of values, brackets, per-slot decimals incl. "first value … second value", '
      'significant digits, decimal-vs-percent form, no-commas, "how many" ⇒ integer, "which year", "absolute") vs. a tokenised answer; '
      'fires on any of: EMPTY, COUNT, NO_BRACKETS, DEC_LONG, DEC_SHORT, SIGFIG, PCT_SIGN_DECIMAL_ASKED, PCT_SIGN_FORBIDDEN, '
      'DECIMAL_FORM_MAGNITUDE, STRAY_TEXT, STRAY_UNIT, COMMAS_FORBIDDEN, YEAR_EXPECTED, NEGATIVE_ABSOLUTE, NON_NUMERIC. '
      '(a\\*) drops the cosmetic STRAY_TEXT/STRAY_UNIT. THOUSANDS_SEP, EXTRA_BRACKETS, STRAY_PERCENT are reported but never fire the checker. '
      '| `checkers.py:174` (spec), `:67` (answer), `:277` (check) |')
    w('| (b) provenance_check | some number in the answer has no output number within half a unit of the answer\'s last typed decimal '
      '(sign-insensitive; thousands separators and CSV pieces parsed) in the full `aggregated_output` of any executed command | `checkers.py:404` |')
    w('| (c) loop_detect | some whitespace-normalised command string is issued for the 3rd time; reports first index, repeats, '
      'longest identical streak, and whether a compaction happened at/after the firing point | `checkers.py:463` |')
    w('| (d) doubt_unresolved | the last 10 reasoning notes contain maybe/unclear/not sure/ambiguous/alternatively/should we, and '
      'no command was executed after the last such note | `checkers.py:517` |')
    w('')

    w('## 2. Headline: all 399 runs')
    w('')
    w(table(MAIN, R))
    w('')
    w('## 3. Submitted answers only (the commit-gate population)')
    w('')
    w(table(MAIN, SUB))
    w('')

    # ---------------- (a) detail
    w('## 4. (a) format_check in detail')
    w('')
    flags = collections.Counter()
    for r in R:
        for k in r['format']['flags']:
            flags[(k, r['correct'], r['arm'])] += 1
    names = sorted({k for k, _, _ in flags}, key=lambda k: (k not in C.HARD_FORMAT_FLAGS, k))
    w('| flag | counted in (a)? | fired wrong | fired right | precision | per arm (wrong/right) |')
    w('|---|---|---|---|---|---|')
    for k in names:
        fw = sum(flags[(k, 0, a)] for a in ARMS)
        fc = sum(flags[(k, 1, a)] for a in ARMS)
        per = ', '.join(f'{a} {flags[(k, 0, a)]}/{flags[(k, 1, a)]}' for a in ARMS)
        role = 'yes' if k in STRICT_FLAGS else ('yes, dropped in (a\\*)' if k in COSMETIC else 'no (soft)')
        w(f'| {k} | {role} | {fw} | {fc} | {pct(fw / (fw + fc)) if fw + fc else "–"} | {per} |')
    w('')
    # decimals applicability
    runs = {(r.arm, r.ordinal): r for r in load.load_runs(with_events=False)}
    applied = passed = 0
    no_spec = 0
    for r in SUB:
        run = runs[(r['arm'], r['ordinal'])]
        spec = C.question_spec(run.question)
        p = C.parse_answer(run.answer, spec.n_values)
        if spec.decimals and len(p.values) == len(spec.decimals):
            applied += 1
            passed += all((v.decimals in spec.decimals[i]) or (spec.year_slot and C._is_year(v)) for i, v in enumerate(p.values))
        elif spec.decimals is None:
            no_spec += 1
    n_q_spec = sum(1 for q in {r.ordinal: r.question for r in runs.values()}.items() if C.question_spec(q[1]).decimals)
    w(f"- The rounding parser extracted a decimals requirement for {n_q_spec}/133 questions. On submitted answers the decimals check "
      f"applied to {applied} runs and **passed in {passed}**; {no_spec} runs had no rounding instruction to check. "
      "GLM essentially never mis-rounds: v8 format failures are not decimals failures.")
    eb = collections.Counter(r['error_bin'] for r in R if not r['correct'])
    eb_f = collections.Counter(r['error_bin'] for r in R if not r['correct'] and fmt_s(r))
    w('- Wrong runs by distance bin (index.json `error_bin`, descriptive) and how many (a\\*) catches: ' +
      ', '.join(f"{k} {eb_f[k]}/{v}" for k, v in eb.most_common()) + '.')
    w("- The five `scale_10^2` wrong runs (Q044 x2, Q049 x3: percent-vs-fraction slips) are **not catchable from the question text**: "
      "neither question states a percent/fraction form, so no format rule fires. A scale check needs the evidence ledger "
      "(e.g. \"answer = 100 x the last calc output\" or a unit field on the candidate).")
    w("- The two COUNT hits are real: one answer was a full sentence with 7 numbers (prompt Q015), one gave a value plus its "
      "percent in parentheses (none Q019); both were graded wrong. This is the one format rule worth keeping as a hard gate, "
      "together with EMPTY (which is L3's job: submit the best ledger candidate at timeout).")
    w("- STRAY_TEXT / STRAY_UNIT / STRAY_PERCENT / THOUSANDS_SEP fire on right runs as often as on wrong ones (e.g. `$` + \"million\" "
      "suffixes, \"percentage points\", `%` on a rate): the grader tolerates them. Bouncing such answers would cost a feedback round "
      "and risk the model changing a correct number.")
    w('')

    # ---------------- (b) detail
    w('## 5. (b) provenance_check in detail (submitted answers)')
    w('')
    rows = [
        ('full outputs (primary)', prov),
        ('model-visible outputs only (Codex-truncated)', lambda r: bool(r['provenance'].get('fired_visible'))),
        ('rescaling-tolerant (x10^±2,3,6,9 allowed)', lambda r: r['provenance']['fired_scaled']),
        ('echo-aware (also fires if only `print(<literal>)` produced it)', lambda r: r['provenance']['fired_echo']),
        ('calc-output only (python/awk/bc output ≤3 KB, not a corpus dump)', lambda r: r['provenance']['fired_calc']),
    ]
    w(compact_table(rows, SUB))
    w('')
    near_w = sum(1 for r in SUB if prov(r) and not r['correct'])
    w("Why it does not separate:")
    w('')
    w("- Of the right runs that fire, most typed the answer after doing small arithmetic in the reasoning (sum/difference/mean of "
      "2–3 table cells, e.g. Q002, Q022, Q050, Q094, Q123) or after a hand unit conversion (thousands→dollars Q040, "
      "millions→billions/trillions Q027, Q048, Q066): GLM's mental arithmetic on a few numbers is usually right.")
    w("- The dominant wrong-run mechanisms in FINDINGS.md (CELL_SELECTION, SOURCE_ISSUE_VINTAGE, INTERPRETATION) produce answers "
      "whose numbers *were* printed by some command, so provenance passes. It only sees arithmetic/transcription slips "
      f"({near_w} wrong submitted runs fire, several of them in the `within_1pct`/`within_0.1pct` bins, e.g. Q045, Q046, Q050).")
    ebw = collections.Counter(r['error_bin'] for r in SUB if not r['correct'])
    ebp = collections.Counter(r['error_bin'] for r in SUB if not r['correct'] and prov(r))
    w('- Wrong submitted runs by distance bin, provenance fired / all: ' + ', '.join(
        f"{k} {ebp[k]}/{v}" for k, v in ebw.most_common()) + '. Firing is spread across bins rather than concentrated in near misses.')
    w("- Visible-vs-full makes almost no difference (the model rarely answers with a number that only existed in a truncated middle). "
      "Requiring the number to come from a *calc* output roughly doubles firing on both sides.")
    w('- Implication: as a gate it should not block; at most, when it fires, ask the model to re-run the final arithmetic in `calc` '
      '(cheap, and harmless when the typed value was right).')
    w('')

    # ---------------- (c) detail
    w('## 6. (c) loop_detect in detail')
    w('')
    rows = [
        ('same command 3x (primary)', loop),
        ('same command + same output 3x', lambda r: r['loop']['fired_same_output']),
        ('same command 3x in a row (streak)', lambda r: r['loop']['fired_consecutive']),
        ('same command ≥10x', lambda r: r['loop']['max_count'] >= 10),
        ('baseline: any compaction happened', lambda r: r['loop']['compactions'] > 0),
        (f'baseline: commands > {q75:.0f} (top quartile)', lambda r: r['loop']['n_cmds'] > q75),
    ]
    w(compact_table(rows, R))
    w('')
    w('Loop runs vs run length (all runs, quartiles of command count):')
    w('')
    qs = np.quantile(n_cmds, [0.25, 0.5, 0.75])
    w('| command-count quartile | loop fired: wrong/right | no loop: wrong/right |')
    w('|---|---|---|')
    for b in range(4):
        sel = [r for r in R if sum(r['loop']['n_cmds'] > t for t in qs) == b]
        a1 = [r for r in sel if loop(r)]
        a0 = [r for r in sel if not loop(r)]
        cnt = lambda xs: f"{sum(1 for r in xs if not r['correct'])}/{sum(1 for r in xs if r['correct'])}"  # noqa: E731
        w(f'| Q{b + 1} | {cnt(a1)} | {cnt(a0)} |')
    w('')
    fired_w = [r for r in fired_loop if not r['correct']]
    fired_c = [r for r in fired_loop if r['correct']]
    w(f"- Fired in {len(fired_loop)} runs; first 3rd-issue at median command {med_at:.0f} ({100 * frac:.0f}% into the run). "
      f"The repeated command is issued a median {np.median([r['loop']['max_count'] for r in fired_loop]):.0f} times "
      f"(max {max(r['loop']['max_count'] for r in fired_loop)}); requiring an identical output as well "
      f"fires on exactly the same runs.")
    w(f"- Compaction: {after_comp}/{len(fired_loop)} fired runs compacted at or after the firing point "
      f"({sum(r['loop']['compacted_after_loop'] for r in fired_w)}/{len(fired_w)} wrong, "
      f"{sum(r['loop']['compacted_after_loop'] for r in fired_c)}/{len(fired_c)} right). "
      f"All {sum(1 for r in R if r['empty'] and loop(r))}/{sum(1 for r in R if r['empty'])} empty (timeout) runs are loop runs.")
    w(f"- Mechanism (measured on the {mech['runs']} runs with an identical-command streak of ≥3): in {mech['same_reason']:,} of "
      f"{mech['pairs']:,} back-to-back repeats the reasoning note before the command was byte-identical too, and the longest streak "
      f"ended at an auto-compaction in {mech['ends_at_compaction']} of {mech['runs']} runs ({mech['ends_at_end']} ran until the run ended, "
      f"{mech['ends_other']} broke on its own). With `temperature=0` the context after each repeat differs only by one more identical "
      "call/output pair, so the model re-emits the same step until compaction rewrites the context. Alternating cycles "
      "(A,B,A,B…) account for the loop runs without a streak.")
    w(f"- It is largely a run-length proxy: a plain \"more than {q75:.0f} commands\" rule and \"any compaction happened\" select almost the "
      "same runs at similar precision. The looping itself is what makes these runs long, so the useful question for v9 is "
      "whether breaking the loop early recovers accuracy, which only an online run can answer. What the replay does show: "
      f"{wasted_all:,} of {total_cmds:,} commands ({100 * wasted_all / total_cmds:.0f}%) were the 3rd-or-later issue of an identical command with "
      f"an identical output (by definition all inside the {len(fired_loop)} loop runs, which issued {loop_cmds:,} commands in total) — pure wasted "
      "budget that also pushed the context into compaction (FINDINGS.md: compaction dropped verified intermediates in 26 failed runs).")
    w(f"- Right runs that fire: {sum(r['loop']['compacted_after_loop'] for r in fired_c)} of {len(fired_c)} recovered after a compaction "
      "and answered correctly; the other repeated one command a few times near the end and then answered (see FP examples). "
      "A loop guard that injects a nudge rather than aborting should not hurt them.")
    w('')

    # ---------------- (d) detail
    w('## 7. (d) doubt_unresolved in detail')
    w('')
    rows = [
        ('any doubt word in last 10 notes (no "unresolved" test)', lambda r: r['doubt']['doubt_in_window']),
        ('last 5 notes, no command after (window 5)', lambda r: r['doubt']['fired_w5']),
        ('last 10 notes, no command after (primary)', doubt),
        ('last 20 notes, no command after (window 20)', lambda r: r['doubt']['fired_w20']),
        ('keyword: no later command mentions a year/number from the doubting notes', lambda r: r['doubt']['fired_keyword']),
    ]
    w(compact_table(rows, R))
    w('')
    w(f"- The window size does not matter: the run's last reasoning note (normally the one written just before the final answer) "
      f"contains a doubt word in {mech['last_note_doubt']} of {mech['with_reason']} runs, so the last doubting note nearly always falls "
      "inside even the 5-note window; windows of 5, 10 and 20 fire on exactly the same runs.")
    w("- Doubt words are near-universal in GLM's reasoning (`maybe` dominates), so the presence of doubt carries no signal; "
      "the \"answered straight after doubting\" pattern is just as common in right runs. The keyword-addressed variant has "
      "higher precision on a tiny number of firings; the interval is too wide to rely on.")
    w("- Consistent with FINDINGS.md (85/103 failures expressed doubt at the divergence point): doubt is present but so is it in "
      "right runs. A doubt-based trigger would need the *content* of the doubt (e.g. two candidate readings) recorded in the "
      "ledger, which L2 would make checkable.")
    w('')

    # ---------------- union
    w('## 8. Union of (a)+(b)+(c)')
    w('')
    rows = [('(a)+(b)+(c)', union), ('(a\\*)+(b)+(c)', union_s), ('(a\\*)+(c) (no provenance)', lambda r: fmt_s(r) or loop(r)),
            ('(c) alone', loop)]
    w('All runs:')
    w('')
    w(compact_table(rows, R))
    w('')
    w('Submitted answers only:')
    w('')
    w(compact_table(rows, SUB))
    w('')
    only = collections.Counter()
    for r in R:
        if not r['correct']:
            key = tuple(n for n, f in (('a', fmt_a), ('b', prov), ('c', loop)) if f(r))
            only[key] += 1
    w('Wrong runs by which checkers fire (all runs): ' + ', '.join(
        f"{'+'.join(k) if k else 'none'} {v}" for k, v in sorted(only.items(), key=lambda kv: -kv[1])) + '.')
    w('')

    # ---------------- FP examples
    w('## 9. False-positive examples (right runs that fire), 5 per checker per arm')
    w('')
    for title, pred, reason in [('(a) format_check', fmt_a, reason_format), ('(b) provenance_check', prov, reason_prov),
                                ('(c) loop_detect', loop, reason_loop), ('(d) doubt_unresolved', doubt, reason_doubt)]:
        w(f'### {title}')
        w('')
        for arm in ARMS:
            ex, n = fp_examples(pred, reason, arm)
            w(f'- **{arm}** ({n} false positives)' + ('' if ex else ': none'))
            for e in ex:
                w(f'  - {e}')
        w('')

    # ---------------- annotations
    w('## 10. Agreement with the divergence analysts (descriptive; 103 analysed failed runs)')
    w('')
    ann = [r for r in R if r['annotation']]
    ct = collections.Counter((r['annotation']['loop'], loop(r)) for r in ann)
    w('- Analyst `loop_or_thrash` vs loop_detect: ' + ', '.join(
        f"{lv}: {ct[(lv, True)]} fire / {ct[(lv, False)]} not" for lv in ['severe', 'mild', 'none']) +
      '. loop_detect captures the severe collapses; "mild" thrash (re-searching with varied queries) is not identical-command '
      'repetition and needs a similarity-based detector if it is to be caught.')
    cd = collections.Counter((r['annotation']['doubt'], r['doubt']['doubt_in_window'], doubt(r)) for r in ann)
    w('- Analyst `glm_expressed_doubt` = yes: ' + f"{sum(v for k, v in cd.items() if k[0] == 'yes' and k[1])} have doubt words in the last 10 notes, "
      f"{sum(v for k, v in cd.items() if k[0] == 'yes' and k[2])} fire (d).")
    st = collections.Counter((r['annotation']['stage'], fmt_s(r) or prov(r)) for r in ann)
    stages = sorted({k for k, _ in st}, key=lambda s: -(st[(s, True)] + st[(s, False)]))
    w('- First-divergence stage vs (a\\*) or (b) firing: ' + ', '.join(f"{s} {st[(s, True)]}/{st[(s, True)] + st[(s, False)]}" for s in stages) + '.')
    w('')

    # ---------------- implications
    w('## 11. What this means for the v9 plan')
    w('')
    w('1. **L3 loop guard: build it; the replay supports it.** Trigger on the 3rd identical command (adding "with identical output" '
      'fires on exactly the same v8 runs). Because the collapse is a temperature-0 fixed point, the guard must change the context (inject a message '
      'with the current best candidate and "that command already returned X; do something different"), not just count. '
      'Also consider a small non-zero temperature for the actor; that is an experimental variable to declare, not a silent fix.')
    w('2. **L3 submit-at-timeout: build it.** All empty answers are loop runs that hit the wall clock; a ledger candidate would at '
      'least be gradable.')
    w('3. **L4 gate: keep only COUNT/EMPTY-type format rules as hard failures.** Do not bounce units, `$`, `%`, commas or '
      '"percentage points" text: they cost right answers nothing under this grader. Decimals rules are cheap to keep but '
      'caught nothing in v8.')
    w('4. **Provenance: advisory only.** It fires on right and wrong runs alike; use it to ask for a `calc` re-run of the final '
      'arithmetic, never to block. Its real use is after L1/L2 exist: "answer = a recorded calc output whose inputs are cited '
      'cells" is checkable against the ledger, and that is where the selection errors live.')
    w('5. **Doubt: drop as a gate.** Revisit only when doubts are recorded as explicit alternative candidates in the ledger.')
    w('6. **Evolver admission test**: this replay harness is the template for the evolver\'s admission test — a proposed checker '
      'must show precision well above the arm base rate on past episodes (these tables), not merely fire on the failure it was written for.')
    w('')
    w('## 12. Limitations')
    w('')
    w('- One run per question per arm; the paired-rerun flip rate is ~27% (FINDINGS.md), so per-arm precision estimates carry the '
      'wide Wilson intervals shown; pooled ALL rows are the ones to read.')
    w('- The question-spec parser is heuristic regex written after reading the 133 question texts (not the answers); it is tuned '
      'to this set and will need generalising for another benchmark.')
    w('- Provenance uses Codex `aggregated_output` (full) and a reconstruction of what the model saw; outputs of commands still '
      'running at a wall-clock kill are missing. Tolerance is half a unit in the answer\'s last typed decimal, sign-insensitive.')
    w('- loop_detect uses exact (whitespace-normalised) command strings; near-duplicate thrash (same intent, different query) is not caught.')
    w('- The effect of *acting* on a checker (nudging, re-running) cannot be measured offline; these numbers bound detection only.')
    w('')
    w('## 13. Reproduce')
    w('')
    w('```')
    w('cd officeqa-v9-design/understand/replay')
    w('/opt/homebrew/bin/python3.11 run_replay.py   # ~15 s, writes results.jsonl (flags only, no answer values)')
    w('/opt/homebrew/bin/python3.11 report.py       # writes ../replay.md')
    w('```')
    w('')
    w('Files: `load.py` (run/trajectory loader, model-visible output reconstruction), `checkers.py` (the four checkers), '
      '`run_replay.py`, `report.py`, `results.jsonl` (per-run flags, counts and value-free reasons).')
    open(OUT, 'w').write('\n'.join(L) + '\n')
    print('wrote', OUT)


if __name__ == '__main__':
    main()
