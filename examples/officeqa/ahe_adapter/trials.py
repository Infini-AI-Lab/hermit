"""OfficeQA's task functions for the AHE Adapter's trial rows (make_rows.py): a row `<UID>#<j>` is the question `<UID>`
(its `uid`) under the id of its j-th rollout (`rollout`). Each function hands ../utils the question's own row, so a
trial's task.md, corpus and score are the question's; any other row (validation, test) passes unchanged.

    agent:
      params:
        prepare: trials.py:prepare        # ../utils/task.py:prepare: task.md and the corpus under docs/
        answer: ../utils/task.py:answer   # unchanged: the last <answer>...</answer>
        collect: trials.py:collect        # ../utils/task.py:collect, then the adapter's hook logs
    judge:
      path: trials.py                     # score: ../utils/judge.py

The adapter's hook runner (hermit/adapter/templates/ahe/hook_runner.py) logs every hook call to
`.hermit-hooks/hook-calls.jsonl` under the session's workspace and each session's state to `.hermit-hooks/hook-state/`;
`collect` copies both to the root of the rollout's log, beside agent-codex.jsonl, prompt.txt, last_message.txt,
deployment.json and task.md, where the learning material keeps them for the AHE Adapter's Evolver.
"""
from __future__ import annotations

import functools
from pathlib import Path
import shutil

from hermit.task import load_callable

UTILS = Path(__file__).resolve().parent.parent / "utils"
HOOK_LOGS = ".hermit-hooks"                 # the hook runner's log base under the session's workspace
AUDIT = "hook-calls.jsonl"
STATE = "hook-state"


@functools.cache
def utils(spec: str):
    """A function of ../utils (`task.py:prepare`), loaded once, as the framework loads a task function."""
    return load_callable(spec, UTILS)


def question(task: dict) -> dict:
    """The question's own row of a trial row: `uid` as its id, without `uid` and `rollout`. Any other row as it is."""
    if "uid" not in task:
        return task
    return {**{k: v for k, v in task.items() if k not in ("uid", "rollout")}, "id": task["uid"]}


def prepare(task: dict, workspace: Path, params: dict, base: Path) -> list[dict]:
    """The question's task.md and the corpus (../utils/task.py:prepare)."""
    return utils("task.py:prepare")(question(task), workspace, params, base)


def _links(directory: str, names: list[str]) -> list[str]:
    """The entries of a directory that are links, which a copy leaves out: the session may have made them, and they
    may lead out of the workspace."""
    return [name for name in names if (Path(directory) / name).is_symlink()]


def collect(task: dict, workspace: Path, log: Path) -> dict:
    """task.md into the log (../utils/task.py:collect), and the hook runner's audit and session states at its root.
    A link in their place is not followed."""
    facts = utils("task.py:collect")(question(task), workspace, log)
    logs = Path(workspace) / HOOK_LOGS
    if logs.is_symlink() or not logs.is_dir():
        return facts
    if (logs / AUDIT).is_file() and not (logs / AUDIT).is_symlink():
        shutil.copy2(logs / AUDIT, Path(log) / AUDIT)
    if (logs / STATE).is_dir() and not (logs / STATE).is_symlink():
        shutil.copytree(logs / STATE, Path(log) / STATE, ignore=_links, dirs_exist_ok=True)
    return facts


def score(task: dict, output) -> dict:
    """The question's score against its reference answer (../utils/judge.py:score)."""
    return utils("judge.py:score")(question(task), output)
