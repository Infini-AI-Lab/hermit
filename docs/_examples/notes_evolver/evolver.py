"""A minimal learning method: the judge's feedback on failed tasks becomes notes in ADAPTER.md.

Each step reads the learning material of the step (one JSON file per task), collects the tasks that scored below 1
with the judge's feedback, and appends them under a "# Notes from feedback" heading in the adapter's ADAPTER.md.
No model is called, so the example runs offline; a real method would ask a model what to write instead.
"""
import json

from harnessadapter.evolver.base import Evolver, StepContext, StepResult

HEADING = "# Notes from feedback"


class NotesEvolver(Evolver):
    def step(self, ctx: StepContext) -> StepResult:
        current = ctx.workspace.learning_material / "current"
        failures = []
        for path in sorted((current / "tasks").glob("*.json")):
            record = json.loads(path.read_text())
            if float(record["judge"]["score"]) < 1.0:
                failures.append(f"- task {record['id']}: {record['judge'].get('feedback') or 'no feedback'}")
        adapter_md = ctx.workspace.adapter / "ADAPTER.md"
        if not failures:
            (ctx.workspace.output / "report.md").write_text("No failures in this step; adapter unchanged.\n")
            return StepResult(status="done", summary="no failures; adapter unchanged")
        text = adapter_md.read_text()
        if HEADING not in text:
            text = text.rstrip("\n") + f"\n\n{HEADING}\n"
        text = text.rstrip("\n") + "\n" + "\n".join(failures) + "\n"
        adapter_md.write_text(text)
        (ctx.workspace.output / "report.md").write_text(f"Added {len(failures)} note(s) to ADAPTER.md.\n")
        return StepResult(status="done", summary=f"added {len(failures)} note(s) to ADAPTER.md")
