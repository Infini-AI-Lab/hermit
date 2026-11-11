""".hermit/CARD.md: the adapter's card, like a model card, generated from the run records."""
from __future__ import annotations

import json

SHOWN = {"full": "every verdict in full (score, feedback, whatever else the judge returned)",
         "score": "each score alone", "none": "no verdict"}


def number(value, digits: int = 3) -> str:
    """A score for the card: `n/a` when nothing was scored."""
    return "n/a" if value is None else f"{value:.{digits}f}"


def render(*, template: dict, run_name: str, parent: str, evolver: str, task: str, epochs_done: int, steps_done: int,
           budget: dict, settings: dict, warnings: list[str], summary: str, eval_results: dict | None = None,
           report: str | None = None, stream: dict | None = None) -> str:
    lines = [f"# Adapter card: {run_name}", "",
             f"- Template: `{template['id']}@{template['version']}`",
             f"- Started from: `{parent}`",
             f"- Evolver: `{evolver}`",
             f"- Task: {task}",
             f"- Training: {epochs_done} epoch(s), {steps_done} step(s)",
             f"- Spending: rollouts ${budget['usd'].get('rollout', 0):.4f}, Evolver ${budget['usd'].get('evolver', 0):.4f}"
             + (f" (+{budget['unknown_cost_calls']} calls of unknown cost)" if budget.get('unknown_cost_calls') else "")
             + f"; {budget['seconds'] / 3600:.2f} h", "",
             "## Settings that affect what can be trusted", ""]
    lines += [f"- sandbox: {json.dumps(settings.get('sandbox'))}",
              f"- environment (the acting agent's sandbox): {json.dumps(settings.get('environment'))}",
              f"- judge: {settings.get('judge_mode') or 'host'}",
              f"- evolver_access: {json.dumps(settings.get('evolver_access'))}",
              f"- validation data given to the Evolver: {settings.get('validation_given')}"]
    run = settings.get("run")
    if run:
        shown = SHOWN.get(run.get("shown") or "full", run.get("shown"))
        lines.append(f"- run mode: {run.get('mode') or 'null (train only)'}; feedback {run.get('feedback') or 'full'}: "
                     f"the learner was shown {shown}"
                     + (" (the Evolver hides the labels from its sessions itself)"
                        if run.get("shown") != (run.get("feedback") or "full") else ""))
    if warnings:
        lines += ["", "Warnings raised at start:", *[f"- {w}" for w in warnings]]
    lines += ["", "## What was learned", "", summary.strip() or "(no summary)"]
    if report:
        lines += ["", f"See the final step's report: `{report}` in the run records."]
    if stream:
        # The rows the Evolver's protocol scored (stream_summary gives it, 0 included, with the rollouts' mean, when
        # the protocol gave a verdict on any row); the other scored rows are their rollouts'. A stream stopped before
        # its last rows never scored them.
        own = stream.get("own_scores")
        whole = stream["reached"] >= stream["rows"]
        lines += ["", "## Stream", "",
                  (f"Each of the stream's {stream['rows']} rows scored once" if whole else
                   f"The stream reached {stream['reached']} of its {stream['rows']} rows, each scored once")
                  + ("" if own is not None else ", by its rollout before the learner saw it") + ": "
                  f"{stream['scored']} scored, {stream['unscored']} not (infra), "
                  f"{stream['rows'] - stream['reached']} not reached. Mean {number(stream['mean'])}; first half "
                  f"{number(stream['first_half'])}, second half {number(stream['second_half'])}."
                  + (f" {own} row(s) scored by the Evolver's own protocol (its step's deliverable, "
                     f"Evolver.step_scores) in place of the rollout, {stream['scored'] - own} by their rollouts "
                     f"before the learner saw them; the rollouts' mean {number(stream.get('rollout_mean'))}."
                     if own is not None else "")
                  + (f" Stopped early: {stream['stopped']}." if stream.get("stopped") else "")]
    lines += ["", "## Evaluation", ""]
    if eval_results:
        lines += ["| Adapter | Tasks | Repeats | Mean score | Solved at least once | Cost (USD) |", "|---|---|---|---|---|---|"]
        for label, r in eval_results.items():
            cost = "unknown" if r.get("cost_usd") is None else f"{r['cost_usd']:.4f}"
            lines.append(f"| {label} | {r['tasks']} | {r['repeats']} | {number(r['mean_score'])} | "
                         f"{number(r['solved_once'])} | {cost} |")
    else:
        lines.append("Not evaluated yet (`hermit eval`).")
    lines += ["", "Where it has not been tested: any task, model or pipeline other than the one above."]
    return "\n".join(lines) + "\n"
