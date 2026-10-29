""".hermit/CARD.md: the adapter's card, like a model card, generated from the run records."""
from __future__ import annotations

import json
from pathlib import Path


def render(*, template: dict, run_name: str, parent: str, evolver: str, task: str, epochs_done: int, steps_done: int,
           budget: dict, settings: dict, warnings: list[str], summary: str, eval_results: dict | None = None,
           report: str | None = None) -> str:
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
              f"- evolver_access: {json.dumps(settings.get('evolver_access'))}",
              f"- validation data given to the Evolver: {settings.get('validation_given')}"]
    if warnings:
        lines += ["", "Warnings raised at start:", *[f"- {w}" for w in warnings]]
    lines += ["", "## What was learned", "", summary.strip() or "(no summary)"]
    if report:
        lines += ["", f"See the final step's report: `{report}` in the run records."]
    lines += ["", "## Evaluation", ""]
    if eval_results:
        lines += ["| Adapter | Tasks | Repeats | Mean score | Solved at least once | Cost (USD) |", "|---|---|---|---|---|---|"]
        for label, r in eval_results.items():
            cost = "unknown" if r.get("cost_usd") is None else f"{r['cost_usd']:.4f}"
            lines.append(f"| {label} | {r['tasks']} | {r['repeats']} | {r['mean_score']:.3f} | "
                         f"{r['solved_once']:.3f} | {cost} |")
    else:
        lines.append("Not evaluated yet (`hermit eval`).")
    lines += ["", "Where it has not been tested: any task, model or pipeline other than the one above."]
    return "\n".join(lines) + "\n"
