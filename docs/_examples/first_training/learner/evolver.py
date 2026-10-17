"""Learn one supported output rule from scored training feedback, offline."""
import json
from harnessadapter.evolver.base import Evolver, StepContext, StepResult


class FormatEvolver(Evolver):
    def step(self, ctx: StepContext) -> StepResult:
        tasks = ctx.workspace.learning_material / "current" / "tasks"
        records = [json.loads(p.read_text()) for p in sorted(tasks.glob("*.json"))]
        rule = "Output format: uppercase."
        learned = any(r["judge"]["score"] < 1 and rule in r["judge"]["feedback"] for r in records)
        if learned:
            (ctx.workspace.adapter / "ADAPTER.md").write_text("# Output policy\n\n" + rule + "\n")
        summary = "learned uppercase output rule" if learned else "kept current output rule"
        (ctx.workspace.output / "report.md").write_text(summary + "\n")
        return StepResult(status="done", summary=summary)
