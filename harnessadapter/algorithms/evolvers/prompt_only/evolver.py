"""PromptOnlyEvolver: each step rewrites configured Markdown files, normally one generation each, with one format retry.

Training State (state/notes.jsonl): one line per step with what was changed, shown to the model at the next step.
"""
from __future__ import annotations

import json
import re

from harnessadapter.evolver.base import Evolver, StepContext, StepResult

SYSTEM = ("You improve the instructions an AI agent receives before it works on a task. You see the current "
          "instructions, how the agent did on recent tasks (its answer, the judge's score and feedback), and notes "
          "from earlier rounds. Write instructions that generalise: methods, checks, pitfalls, output conventions. "
          "Never copy facts or answers of individual tasks into the instructions.")


def extract(text: str) -> str | None:
    """The file between <<<FILE and FILE>>>; tolerant of a missing or reworded closing marker (then up to SUMMARY:)."""
    m = re.search(r"<<<FILE[ \t]*\n?(.*?)\n?[ \t]*(?:FILE>>>|>>>FILE|<<<END>>>|END FILE>>>)", text, re.S)
    if m and m.group(1).strip():
        return m.group(1).strip()
    start = text.find("<<<FILE")
    if start < 0:
        return None
    body = text[start + len("<<<FILE"):]
    body = re.split(r"\n\s*SUMMARY:", body)[0]
    body = re.sub(r"\n\s*(?:FILE)?>>>\s*$", "", body.rstrip())
    return body.strip() or None


def _clip(value, n):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= n else text[:n] + " …[truncated]"


class PromptOnlyEvolver(Evolver):
    def step(self, ctx: StepContext) -> StepResult:
        if "llm" not in ctx.tools:
            return StepResult("failed", "prompt_only needs the run's llm tool: set `llm` in run.yaml")
        p = ctx.params
        current = ctx.workspace.learning_material / "current"
        records = [json.loads(f.read_text()) for f in sorted((current / "tasks").glob("*.json"))]
        records.sort(key=lambda r: r["judge"]["score"])
        shown = records[: int(p.get("max_examples", 8))]
        n = int(p.get("max_chars", 3000))
        examples = "\n\n".join(
            f"### Task {r['id']} — score {r['judge']['score']}\nInput: {_clip(r['input'], n)}\n"
            f"Agent's final output: {_clip(r['output'], n)}\nJudge feedback: {_clip(r['judge'].get('feedback', ''), n)}"
            for r in shown)
        feedback = current / "check_feedback.json"
        repaired = f"\nThe Trainer had to repair your last version: {feedback.read_text()}\n" if feedback.exists() else ""
        notes_file = ctx.workspace.state / "notes.jsonl"
        notes = notes_file.read_text().strip().splitlines()[-5:] if notes_file.exists() else []
        guide = ctx.workspace.adapter / "EVOLVER.md"
        task_readme = ctx.workspace.task_info / "README.md"
        changed = []
        for name in p.get("files", ["ADAPTER.md"]):
            path = ctx.workspace.adapter / name
            old = path.read_text() if path.exists() else ""
            prompt = (f"## The task\n{task_readme.read_text() if task_readme.exists() else '(no description)'}\n\n"
                      f"## The adapter's guide\n{guide.read_text() if guide.exists() else ''}\n\n"
                      f"## Current {name}\n<<<\n{old}\n>>>\n\n"
                      f"## Results of this step (mean score "
                      f"{sum(r['judge']['score'] for r in records) / max(len(records), 1):.3f} on {len(records)} tasks)"
                      f"\n{examples}\n{repaired}\n## Notes from earlier rounds\n" + ("\n".join(notes) or "(none)") +
                      f"\n\nRewrite {name}. Reply with the complete new file between <<<FILE and FILE>>>, then one line "
                      "starting with SUMMARY: that says what you changed and why.")
            messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]
            reply = ctx.tools["llm"](messages=messages, max_tokens=int(p.get("max_tokens", 32000)),
                                     temperature=float(p.get("temperature", 0.3)))
            text = reply["content"]
            replies = [text]
            body = extract(text)
            if body is None:
                # A reasoning model may spend its budget thinking (empty content) or be cut off mid-file
                # (finish_reason "length"): ask once more for the file alone.
                messages += [{"role": "assistant", "content": text},
                             {"role": "user", "content": "Your reply did not contain the complete file. Reply now "
                              f"with only the complete new {name} between <<<FILE and FILE>>>, then the SUMMARY: "
                              "line. No analysis."}]
                reply = ctx.tools["llm"](messages=messages, max_tokens=int(p.get("max_tokens", 32000)),
                                         temperature=float(p.get("temperature", 0.3)))
                text = reply["content"]
                replies.append(text)
                body = extract(text)
            log = ctx.workspace.output / "llm_replies"
            log.mkdir(exist_ok=True)
            for i, t in enumerate(replies):
                (log / f"{name.replace('/', '_')}.{i}.txt").write_text(t)
            summary = (re.search(r"SUMMARY:\s*(.+)", text) or [None, "no summary"])[1].strip()
            if body is None:
                continue
            path.write_text(body + "\n")
            changed.append((name, summary))
        check = ctx.tools["check_spec"]()
        if not check["passed"]:     # handed back as is: the Trainer repairs it and says so in the next step
            summary = "rewrite failed check_spec: " + "; ".join(i["problem"] for i in check["issues"][:3])
        else:
            summary = "; ".join(f"{n}: {s}" for n, s in changed) or "no change (the model gave no usable file)"
        with open(notes_file, "a") as f:
            f.write(json.dumps({"step": ctx.progress.step if ctx.progress else None, "summary": summary}) + "\n")
        (ctx.workspace.output / "report.md").write_text(summary + "\n")
        return StepResult("done", summary[:300], usage=[])   # usage of the llm tool is metered by the Trainer
