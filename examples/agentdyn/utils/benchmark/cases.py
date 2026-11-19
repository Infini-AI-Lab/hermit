"""A protocol's frozen cases as task directories (Harbor's format), shared by AgentDyn's and AgentDojo's prepare
scripts:

    <protocol>/tasks/<id>/   instruction.md (the case in a line) and task.toml, whose [metadata] `case` is the case's
                             row as one JSON line (TOML holds no null, which a clean case's injection_task_id is)
    <protocol>/cases.txt     every case's id, in the order the rows were written (tasks.jsonl's lines before)
    <protocol>/train.txt ... the splits, and manifest.json, written by the prepare script

`case_row` is the configs' data.view: a case's task directory read back as exactly the row tasks.jsonl held, so the
agent loop, the judge, the records and the learning material are what they were. `write_cases` returns the SHA-256
of the rows as JSON lines (the bytes tasks.jsonl held), which the manifests keep as `tasks_sha256` and DATA.md lists.
A case asks for no network (its environment is AgentDyn's or AgentDojo's own simulation; the victim's model is the
run's) and has no tests/: the judge (judge.py) reads the native verdicts from the agent's output.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

from hermit.task.directory import write_task

NAMES = {"agentdyn": "AgentDyn", "agentdojo": "AgentDojo"}


def case_row(task: dict) -> dict:
    """data.view: a case's task directory as its row (the row as it is when it is one already)."""
    if "metadata" not in task:
        return dict(task)
    return json.loads(task["metadata"]["case"])


def instruction(row: dict, benchmark: str) -> str:
    text = f"{NAMES.get(benchmark, benchmark)} {row['suite']} user task `{row['user_task_id']}`"
    if row.get("injection_task_id"):
        text += f", with the frozen injection of `{row['injection_task_id']}`"
    return text + ": complete the user's task in the suite's native environment."


def write_cases(output: Path, rows: list[dict], *, benchmark: str, sort_keys: bool = False) -> str:
    """Every row as a task directory under output/tasks (made anew) and their order in output/cases.txt; the SHA-256
    of the rows as JSON lines (`sort_keys` as the JSON lines were written)."""
    output = Path(output)
    # As the JSON lines read back: sort_keys sorts every mapping's keys, the injections' too
    rows = [json.loads(json.dumps(r, sort_keys=sort_keys)) for r in rows]
    if (output / "tasks").exists():
        shutil.rmtree(output / "tasks")
    for row in rows:
        write_task(output / "tasks" / row["id"], instruction(row, benchmark), metadata={"case": json.dumps(row)},
                   environment={"network_mode": "no-network"})
    (output / "cases.txt").write_text("".join(r["id"] + "\n" for r in rows))
    return hashlib.sha256("".join(json.dumps(r) + "\n" for r in rows).encode()).hexdigest()


def read_cases(protocol: Path) -> list[dict]:
    """A protocol's rows in their order: its task directories through case_row (cases.txt's order), or the
    tasks.jsonl of a protocol prepared before the task directories."""
    protocol = Path(protocol)
    if (protocol / "tasks").is_dir():
        from hermit.task.directory import tasks_of
        found = {tid: t.given() for tid, t in tasks_of(protocol / "tasks", view=case_row).items()}
        order = [i for i in (protocol / "cases.txt").read_text().splitlines() if i.strip()] \
            if (protocol / "cases.txt").is_file() else sorted(found)
        return [found[i] for i in order]
    return [json.loads(s) for s in (protocol / "tasks.jsonl").read_text().splitlines() if s.strip()]


def rows(path: Path) -> list[dict]:
    """The rows a report reads: of a protocol directory, of its tasks/ directory, or of a tasks.jsonl."""
    path = Path(path)
    if path.is_file():
        return [json.loads(s) for s in path.read_text().splitlines() if s.strip()]
    return read_cases(path.parent if path.name == "tasks" and not (path / "tasks").is_dir() else path)
