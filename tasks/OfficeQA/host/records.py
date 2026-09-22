"""The learner's evidence for OfficeQA, in the record shape the mwm builtins
read (one JSON file per episode under evidence/blockNNN/, addressed by its
stem), with the per-episode feedback the controller computes from the
official grader after the batch is sealed.

Nothing here runs a model. The grader and the answer key are controller-only.
"""
from __future__ import annotations

import json
from pathlib import Path

from paths import DEPLOYMENT, VENDOR

SUBAGENT_NOTE = ("Sub-agent turns are not part of this venue's records: the hosts here "
                 "do not start sub-agents.")
CLIP = 2000


def question_id(ordinal: int, task_id: str) -> str:
    return f"q{ordinal:03d}_id{task_id}"


def clip(text: str) -> str:
    if not isinstance(text, str) or len(text) <= CLIP:
        return text
    return text[:CLIP] + f"\n[... {len(text) - CLIP} characters left out of this record]"


def public_trajectory(events_path: Path) -> list:
    """Completed messages, commands and tool calls; no model reasoning."""
    import broker
    rows = []
    for line in Path(events_path).read_text(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        item = event.get("item", {}) if isinstance(event, dict) else {}
        if event.get("type") == "item.completed" and item.get("type") in (
                "agent_message", "command_execution", "mcp_tool_call", "file_change"):
            row = broker.public(item)
            for key in ("aggregated_output", "output", "result"):
                if isinstance(row.get(key), str):
                    row[key] = clip(row[key])
                elif isinstance(row.get(key), dict):
                    for inner in ("content", "text"):
                        if isinstance(row[key].get(inner), str):
                            row[key][inner] = clip(row[key][inner])
            rows.append(row)
    return rows


def grader():
    import importlib.util
    spec = importlib.util.spec_from_file_location("officeqa_reward", VENDOR / "reward.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.score_answer


def feedback(reference: str, answer: str, mode: str) -> dict:
    """What the record says about the outcome. mode: 'verdict' (correct /
    incorrect at tolerance 0), 'gold' (the verdict and the official answer),
    'none' (nothing)."""
    if mode == "none":
        return {}
    score = grader()
    strict = int(score(reference, answer, tolerance=0.0))
    out = {"verdict": "CORRECT" if strict else "INCORRECT"}
    if mode == "gold":
        out["reference_answer"] = reference
    return out


def write_record(evidence_dir: Path, block: int, ordinal: int, task: dict, prediction: dict,
                 summary: dict, events_path: Path, fb: dict) -> str:
    bdir = Path(evidence_dir) / f"block{block:03d}"
    bdir.mkdir(parents=True, exist_ok=True)
    name = question_id(ordinal, task["task_id"]) + ".json"
    trajectory = public_trajectory(events_path)
    if fb.get("verdict"):
        # As in the database venue: the grader's verdict closes the episode
        # as the last observation; the host does not act after it.
        text = f"Question {ordinal}: " + ("CORRECT!" if fb["verdict"] == "CORRECT" else "INCORRECT.")
        text += f"\nYour answer: {prediction.get('answer', '') or '(none)'}"
        if fb["verdict"] != "CORRECT" and "reference_answer" in fb:
            text += f"\nCorrect answer: {fb['reference_answer']}"
        trajectory.append({"type": "environment_feedback", "text": text})
    record = {
        "question_id": question_id(ordinal, task["task_id"]),
        "question": task["question"],
        "answer": prediction.get("answer", ""),
        "status": summary.get("status"),
        "elapsed_seconds": summary.get("elapsed_seconds"),
        "requests": summary.get("requests"),
        "trajectory": trajectory,
        "feedback": fb,
    }
    (bdir / name).write_text(json.dumps(record, ensure_ascii=False, indent=1) + "\n")
    return f"/evidence/block{block:03d}/{name}"


def one_question(ordinal: int, task: dict, prediction: dict, summary: dict, fb: dict, path: str) -> str:
    lines = [f"### Question {ordinal}",
             "question: " + " ".join(task["question"].split())[:600],
             f"answer given: {prediction.get('answer', '') or '(none)'}",
             f"status: {summary.get('status')}; requests: {summary.get('requests')}"]
    if fb.get("verdict"):
        lines.append("outcome: " + fb["verdict"] + (f" — official answer: {fb['reference_answer']}" if "reference_answer" in fb else ""))
    lines.append(f"raw record: {path}")
    return "\n".join(lines)


def readme_text() -> str:
    return (DEPLOYMENT / "contract" / "env_evidence.md").read_text().rstrip("\n") + "\n\n" + SUBAGENT_NOTE + "\n"


def write_readme(evidence_dir: Path) -> None:
    Path(evidence_dir).mkdir(parents=True, exist_ok=True)
    (Path(evidence_dir) / "README.md").write_text(readme_text())


def render(block: int, episodes: list, evidence_dir: Path) -> str:
    """The block's evidence section: one entry per question with its record
    address; no block-level statistics."""
    write_readme(evidence_dir)
    body = ["=" * 70]
    for ep in episodes:
        path = write_record(evidence_dir, block, ep["ordinal"], ep["task"], ep["prediction"],
                            ep["summary"], ep["events"], ep["feedback"])
        body += [one_question(ep["ordinal"], ep["task"], ep["prediction"], ep["summary"], ep["feedback"], path), "=" * 70]
    return "\n".join(body)
