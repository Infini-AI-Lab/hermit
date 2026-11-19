"""An eval of a Harbor benchmark, read from its results: pass@1 over the trials that are the agent's result.

    python -m hermit.harbor.report <eval dir or results.jsonl>... [--json]

`hermit eval` scores a trial that is not the agent's result (infra: the task's environment could not be given, or it
failed under the verifier) as a failed run: its judge (hermit.task.verify.environment) gives it no score, so it counts
among the summary's `errors` and as 0 in its `mean_score`. A benchmark's own figure leaves such trials out. This reads
the eval's results.jsonl (`<run dir>/eval/<adapter>/`) and gives, over all its repeats and for each repeat:

    trials        the rows of the eval
    scored        the trials the judge scored (status ok): the agent's results
    infra         the trials that are not the agent's result (an environment that could not be given, or that failed:
                  the judge's Unscored)
    failed        the other failed runs (the pipeline or the judge failed for another reason)
    pass_at_1     of the scored trials, the share that passed (the judge's `passed`, which hermit eval keeps in the
                  row's judge.extra: the reward reached the run's pass_reward); null when the judge says no trial
                  passes or fails (pass_reward: null, a score scale)
    mean_scored   the scored trials' mean score
    mean_all      every trial's mean score, the failed and the infra ones counted 0 (hermit eval's mean_score)

An eval that did not finish (a lost sign-in stopped it: LoginFailed, or anything else) wrote no row of the repeat it
stopped in (hermit.trainer.rollout.run_tasks hands back a repeat's records once all of them are done), so its
results.jsonl holds the rows of the repeats it finished, none with one repeat; and `hermit eval` writes its
summary.json when it ends, so there is none beside them, or the summary of the eval before it; an eval stopped as it
wrote its rows leaves the last one cut off, which is left out. The report says so (`unfinished`, the reason; the
JSON's `finished` false), reading the summary's content, never the files' times, which a copy changes. Content alone
cannot tell an eval stopped in a later repeat whose finished ones match the summary of the eval before (as many rows,
failed runs and the same mean: a smoke run again with more repeats, say): where results.jsonl was written more than
SKEW_S after summary.json, which a finished eval in place never does, the report notes it (`note`). Nothing is run
again here; a rerun of the infra trials, or of an unfinished eval, is an eval run again.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

RESULTS = "results.jsonl"
SUMMARY = "summary.json"
SKEW_S = 60.0                      # results.jsonl written this long after summary.json: no finished eval in place


def read_rows(path: Path) -> tuple[list[dict], bool]:
    """(the rows of an eval's results.jsonl, `path` or the file in the eval directory `path`; whether its last line is
    cut off: no JSON, and no line end after it, as an eval stopped while it wrote that row leaves it). Lines end at
    "\n" alone, as hermit eval writes them; any other line that is no JSON raises ValueError."""
    path = Path(path)
    if path.is_dir():
        path = path / RESULTS
    lines = path.read_text(encoding="utf-8").split("\n")
    rows, cut = [], False
    for n, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except (ValueError, RecursionError):
            if n == len(lines) - 1:                         # the last line, no line end after it: cut off
                cut = True
                continue
            raise ValueError(f"line {n + 1} of {path} is no JSON row") from None
        if isinstance(row, dict):
            rows.append(row)
    return rows, cut


def rows_of(path: Path) -> list[dict]:
    """The rows of an eval's results.jsonl (read_rows), a last row cut off left out."""
    return read_rows(path)[0]


def unfinished(path: Path, rows: list[dict]) -> str | None:
    """Why the rows of the eval at `path` are not those of a finished eval (the module's docstring), or None: no
    summary.json beside them, one that cannot be read, or one that is not of them: it counts other trials (its tasks
    times its repeats), or other failed runs (`errors`, the rows whose status is not ok) or another mean score
    (`mean_score`, of every row's score) than the rows hold, as the summary of the eval before does."""
    path = Path(path)
    results = path / RESULTS if path.is_dir() else path
    try:
        written = json.loads((results.parent / SUMMARY).read_text(encoding="utf-8"))
        tasks, repeats = int(written["tasks"]), int(written["repeats"])
        errors = None if written.get("errors") is None else int(written["errors"])
        mean = None if written.get("mean_score") is None else float(written["mean_score"])
    except FileNotFoundError:
        return f"no {SUMMARY} beside its rows: the eval did not finish"
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return f"its {SUMMARY} cannot be read as an eval's summary"
    if tasks * repeats != len(rows):
        return (f"its {SUMMARY} counts {tasks * repeats} trials and its {RESULTS} holds {len(rows)}: the eval did not "
                "finish")
    scores = [float((row.get("judge") or {}).get("score") or 0.0) for row in rows]
    if (errors is not None and errors != sum(1 for row in rows if row.get("status") != "ok")) or \
            (mean is not None and abs(mean - (sum(scores) / len(scores) if scores else 0.0)) > 1e-9):
        return (f"its {SUMMARY} is not of its rows (another count of failed runs, or another mean score): the eval "
                "before's, and this one did not finish")
    return None


def later(path: Path) -> str | None:
    """A note when the eval's results.jsonl was written more than SKEW_S after its summary.json, which `hermit eval`
    never does in place (the summary is written last): a copy of the folder, or an eval stopped beside the summary of
    the eval before; None otherwise, or without both files."""
    path = Path(path)
    results = path / RESULTS if path.is_dir() else path
    try:
        gap = results.stat().st_mtime - (results.parent / SUMMARY).stat().st_mtime
    except OSError:
        return None
    if gap <= SKEW_S:
        return None
    return (f"its {RESULTS} was written {gap:.0f} s after its {SUMMARY}: unless the folder was copied, the eval did "
            "not finish, beside the summary of the eval before")


INFRA_SAID = ("Unscored", "the task's environment could not be given")


def kind(row: dict) -> str:
    """scored, infra or failed (the module's docstring)."""
    if row.get("status") == "ok":
        return "scored"
    judged = row.get("judge") if isinstance(row.get("judge"), dict) else {}
    said = f"{row.get('error') or ''} {judged.get('feedback') or ''}"
    return "infra" if any(word in said for word in INFRA_SAID) else "failed"


def passed(row: dict):
    """Whether the judge said the trial passed (True, False; None: no pass reward), from the row's judge record as
    hermit eval writes it: the judge's other fields beside score and feedback are its `extra`
    (hermit.task.normalise_judge)."""
    judged = row.get("judge") if isinstance(row.get("judge"), dict) else {}
    extra = judged.get("extra") if isinstance(judged.get("extra"), dict) else {}
    return extra.get("passed")


def summary(rows: list[dict]) -> dict:
    """The figures of the module's docstring for `rows`."""
    kinds = [kind(r) for r in rows]
    scored = [r for r, k in zip(rows, kinds) if k == "scored"]
    scores = [float((r.get("judge") or {}).get("score") or 0.0) for r in scored]
    passes = [passed(r) for r in scored]
    judged = [p for p in passes if p is not None]
    return {"trials": len(rows), "scored": len(scored), "infra": kinds.count("infra"), "failed": kinds.count("failed"),
            "pass_at_1": sum(1 for p in judged if p) / len(judged) if judged else None,
            "mean_scored": sum(scores) / len(scores) if scores else None,
            "mean_all": sum(scores) / len(rows) if rows else None}


def report(path: Path) -> dict:
    """An eval's figures: over all its rows, and by repeat when it has more than one; a last row cut off (read_rows)
    is left out, and the eval did not finish."""
    rows, cut = read_rows(path)
    why = f"the last row of its {RESULTS} is cut off: the eval stopped as it wrote it" if cut else \
        unfinished(path, rows)
    out = {"results": str(path), **summary(rows), "finished": why is None}
    if why is not None:
        out["unfinished"] = why
    elif (note := later(path)) is not None:
        out["note"] = note
    repeats = sorted({r.get("repeat") for r in rows if r.get("repeat") is not None})
    if len(repeats) > 1:
        out["by_repeat"] = {str(n): summary([r for r in rows if r.get("repeat") == n]) for n in repeats}
    return out


def _figure(value, percent: bool = False) -> str:
    if value is None:
        return "-"
    return f"{100 * value:.1f}%" if percent else f"{value:.4f}"


def line(figures: dict) -> str:
    return (f"{figures['trials']} trials: {figures['scored']} scored, {figures['infra']} infra, {figures['failed']} "
            f"failed; pass@1 {_figure(figures['pass_at_1'], True)} of the scored, mean score "
            f"{_figure(figures['mean_scored'])} of the scored, {_figure(figures['mean_all'])} with the infra and "
            "failed trials as 0")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("evals", nargs="+", type=Path, help="an eval's directory or its results.jsonl")
    parser.add_argument("--json", action="store_true", help="the figures as JSON, one object per eval")
    args = parser.parse_args(argv)
    for path in args.evals:
        try:
            figures = report(path)
        except (OSError, ValueError) as exc:
            print(f"{path}: {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(figures))
            continue
        print(f"{path}: {line(figures)}")
        if figures.get("unfinished"):
            print(f"  unfinished: {figures['unfinished']}; the figures are of the repeats it finished, if any")
        if figures.get("note"):
            print(f"  note: {figures['note']}")
        for n, each in (figures.get("by_repeat") or {}).items():
            print(f"  repeat {n}: {line(each)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
