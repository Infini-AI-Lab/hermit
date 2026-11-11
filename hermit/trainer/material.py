"""Learning material: one directory per step in one standard format, index.jsonl, a `current` link.

What the learner is shown of each verdict follows the run's feedback policy (`run.feedback`, `shown` and
`hide_label_files` below, which the Trainer's run_agent results use too): `full` keeps every record as the rollout
made it; `score` keeps the judge's score alone; `none` keeps no score. Under `score` and `none` a record also loses
the reward and verdict fields a pipeline carries beside the judge (a Harbor trial's `output.reward`, `rewards`, its
status, `pipeline_extra.verifier_reward_raw`, ...; its `pipeline_extra.timed_out` reads false when the time ran out
on the verifier, a solution too slow for it, and stays as it is when it ran out on the agent's session), and a log
directory loses the files that hold the verifier's output or the trial's result (a curated Harbor log's `verifier/`,
`result.json`, `harbor.log`, `exception.txt`, a stream step's `driver.log`). A task's own fields (`input`) are kept as
they are: a task keeps its answers out of the rows it gives the agent.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import textwrap

from hermit.isolate import _safe
from hermit.trainer.rollout import mean_score, scored

POLICIES = ("full", "score", "none")
# A log directory's files that carry the verdict: Harbor's curated log (hermit/harbor/curate.py) keeps the verifier's
# output and reward files in verifier/, the trial's result (rewards, exception) in result.json, the job's reward table
# in harbor.log, the exception in exception.txt, and a stream step's driver.log says each step's reward.
LABEL_FILES = ("verifier", "result.json", "harbor.log", "exception.txt", "driver.log")
OUTPUT_VERDICT = ("reward", "rewards")                             # any output mapping's
HARBOR_VERDICT = ("status", "failure_cause", "exception_type")     # a Harbor trial's output (it names `harbor_task`)
# pipeline_extra's: Harbor's verdict (hermit.harbor.runner._output), the raw job (its full verifier output), the
# judge's settings, and whether a multi-step task's step ended its stream (`aborted`: scored under its min_reward)
EXTRA_VERDICT = ("verifier_reward_raw", "failure_cause", "status", "exception_type", "exception_message",
                 "harbor_job", "judge", "aborted")
# A Harbor trial's failure cause when its verifier ran out of time on the agent's solution (verifier_timeout: agent,
# hermit.harbor.runner.verifier_timeout_verdict): its `timed_out` is that verdict, not how the session ended; so is the
# timed_out of one whose exception is the verifier's timeout though a session fact gave it another cause (infra)
VERIFIER_TIMED_OUT = "solution_timed_out"
VERIFIER_TIMEOUT_ERROR = "VerifierTimeoutError"

def readme(policy: str = "full") -> str:
    """The material's README.md: what it holds under the run's feedback policy (learner-facing)."""
    verdict = policy != "none"
    intro = {"full": None,
             "score": "What you are shown of each task's verdict is its score alone: no feedback text, no verifier "
                      "output, no reward fields besides the score.",
             "none": "You are shown no verdict on any task: no score, no feedback, no verifier output, no reward "
                     "fields. What the agent did (its output and its trajectory) is your evidence."}[policy]
    index = ("One directory per training step (`step_0001/`, ...); `current` points at the latest; `index.jsonl` has "
             "one line per step: step, epoch, number of tasks, "
             + ("mean score, how many tasks were not scored, " if verdict else "")
             + "rollout cost, and whether the adapter used for these rollouts differed from the previous step's.")
    summary = ("`summary.json` — step, epoch, the adapter commit the rollouts used, task ids, "
               + ("mean score, cost, time. A task that was not scored (status `infra`) is left out of the mean and "
                  "counted in `unscored`." if verdict else "cost, time."))
    output = ("`output` (what the agent produced)" if policy == "full" else
              "`output` (what the agent produced, less any reward field and a Harbor trial's verifier status)")
    judge = {"full": "`judge` (everything the judge returned: score, feedback, anything else), ",
             "score": "`judge` (the score alone), ", "none": ""}[policy]
    status = ("`status` (`ok`; `error` or `judge_error`: the run or its judge failed, scored 0; `infra`: not the "
              "agent's result, the machine or the model provider failed, not scored)" if verdict else
              "`status` (`ok`; `error` or `judge_error`: the run or its judge failed; `infra`: the run failed for a "
              "reason that is not the agent's, the machine or the model provider)")
    tasks = (f"`tasks/<task id>.json` — for each task: `input` (the task as given to the pipeline), {output}, {judge}"
             f"`cost_usd` (null\xa0=\xa0unknown), `wall_s`, `api_speed`, `tokens`, {status}, `error`, `trajectory` "
             f"(where the {'full ' if policy == 'full' else ''}trajectory is) and `messages` (a normalised message "
             "list, when the pipeline provides one).")
    trajectories = ("`trajectories/<task id>/` — the host's original log, copied unchanged." if policy == "full" else
                    "`trajectories/<task id>/` — the host's log, without the verifier's output and the run's result "
                    "and exception files.")
    check = ("`check_feedback.json` — present when the adapter the previous step handed back failed check_spec: each "
             "repaired file, where it was restored from, and why.")
    wrap = lambda text, indent="": textwrap.fill(text, 116, initial_indent=indent, subsequent_indent=" " * len(indent),
                                                 break_on_hyphens=False, break_long_words=False)
    parts = ["# Learning material", *([wrap(intro)] if intro else []), wrap(index), "Each step directory:",
             "\n".join(wrap(item, "- ") for item in (summary, tasks, trajectories, check))]
    return "\n\n".join(parts).replace("\xa0", " ") + "\n"          # a no-break space keeps a phrase on one line


README = readme("full")


def shown(record: dict, policy: str) -> dict:
    """A run record as the learner is shown it under `policy` (full: as it is)."""
    if policy == "full":
        return record
    out = dict(record)
    judge = record.get("judge")
    if policy == "score" and isinstance(judge, dict):
        out["judge"] = {"score": judge.get("score")}
    else:
        out.pop("judge", None)
    if record.get("status") == "judge_error":               # the judge's own exception may name the answer
        out["error"] = f"the judge failed (its message is not shown: run.feedback is {policy})"
    output = record.get("output")
    if isinstance(output, dict):
        drop = OUTPUT_VERDICT + (HARBOR_VERDICT if output.get("harbor_task") else ())
        out["output"] = {k: v for k, v in output.items() if k not in drop}
    extra = record.get("pipeline_extra")
    if isinstance(extra, dict):
        out["pipeline_extra"] = {k: v for k, v in extra.items() if k not in EXTRA_VERDICT}
        if "timed_out" in extra and (extra.get("failure_cause") == VERIFIER_TIMED_OUT
                                     or extra.get("exception_type") == VERIFIER_TIMEOUT_ERROR):
            out["pipeline_extra"]["timed_out"] = False       # as for a run that did not time out
    speed = record.get("api_speed")
    if isinstance(speed, dict) and "status" in speed:         # a Harbor trial's api_speed names its status
        out["api_speed"] = {k: v for k, v in speed.items() if k != "status"}
    return out


def hide_label_files(log: Path, policy: str) -> None:
    """Remove, from a copy of a log directory, the files that carry the verdict (LABEL_FILES), unless `policy` is
    full. A log that is one file is the agent's own and stays."""
    log = Path(log)
    if policy == "full" or not log.is_dir() or log.is_symlink():
        return
    for name in LABEL_FILES:
        path = log / name
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.is_symlink() or path.exists():
            path.unlink()


class Material:
    def __init__(self, root: Path, policy: str = "full"):
        if policy not in POLICIES:
            raise ValueError(f"the feedback policy is one of {', '.join(POLICIES)}, not {policy!r}")
        self.root = Path(root)
        self.policy = policy
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "README.md").write_text(readme(policy))     # the run's policy, a resumed run's included

    def step_dir(self, step: int) -> Path:
        return self.root / f"step_{step:04d}"

    def write(self, step: int, epoch: int, commit: str, changed: bool, results: list[dict],
              check_feedback: list[dict] | None) -> Path:
        d = self.step_dir(step)
        if d.exists():
            shutil.rmtree(d)
        (d / "tasks").mkdir(parents=True)
        costs = [r["cost_usd"] for r in results if r.get("cost_usd") is not None]
        for r in results:
            record = shown(dict(r), self.policy)
            log = r.get("log")
            # A link is copied as a link, never followed: the log is the agent's, and what it links to need not be.
            if log and Path(log).exists() and not Path(log).is_symlink():
                target = d / "trajectories" / _safe(r["id"])
                if Path(log).is_dir():
                    shutil.copytree(log, target, symlinks=True)
                    hide_label_files(target, self.policy)
                else:
                    target.mkdir(parents=True)
                    shutil.copy2(log, target / Path(log).name, follow_symlinks=False)
                record["trajectory"] = os.path.relpath(target, d)
            else:
                record["trajectory"] = None
            record.pop("log", None)
            (d / "tasks" / f"{_safe(r['id'])}.json").write_text(json.dumps(record, ensure_ascii=False, indent=1,
                                                                           default=str))
        summary = {"step": step, "epoch": epoch, "adapter_commit": commit, "adapter_changed": changed,
                   "tasks": [r["id"] for r in results]}
        if self.policy != "none":            # the step's verdict: none of it under `none`
            summary |= {"mean_score": mean_score(results), "unscored": len(results) - len(scored(results))}
        summary |= {"cost_usd": sum(costs) if costs else None,
                    "unknown_cost_tasks": sum(1 for r in results if r.get("cost_usd") is None),
                    "wall_s": max((r.get("wall_s") or 0) for r in results) if results else 0}
        (d / "summary.json").write_text(json.dumps(summary, indent=1))
        if check_feedback:
            (d / "check_feedback.json").write_text(json.dumps(check_feedback, indent=1))
        line = {k: summary[k] for k in ("step", "epoch", "mean_score", "unscored", "cost_usd", "adapter_changed")
                if k in summary}
        with open(self.root / "index.jsonl", "a") as index:
            index.write(json.dumps(line | {"tasks": len(results)}) + "\n")
        current = self.root / "current"
        if current.is_symlink() or current.exists():
            current.unlink()
        current.symlink_to(d.name)
        from hermit.redact import redact_tree
        redact_tree(d)                    # the agents held their keys: none of them reaches the learner's material
        return d

    def truncate(self, last_step: int) -> None:
        """Drop material of steps after `last_step` (a discarded step on resume)."""
        for d in self.root.glob("step_*"):
            if int(d.name.split("_")[1]) > last_step:
                shutil.rmtree(d)
        index = self.root / "index.jsonl"
        if index.exists():
            # "\n" alone ends a line: U+0085, U+2028 and U+2029 end none (as in Trainer._completed_steps)
            keep = [l for l in index.read_text().split("\n") if l and json.loads(l)["step"] <= last_step]
            index.write_text("".join(l + "\n" for l in keep))
        current = self.root / "current"
        if current.is_symlink():
            current.unlink()
        if last_step > 0 and self.step_dir(last_step).exists():
            current.symlink_to(self.step_dir(last_step).name)
