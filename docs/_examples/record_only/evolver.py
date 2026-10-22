"""An offline example of the Evolver contract; records evidence without optimizing."""
import json

from hermit.evolver.base import Evolver, StepContext, StepResult


class RecordOnlyEvolver(Evolver):
    def step(self, ctx: StepContext) -> StepResult:
        summary_path = ctx.workspace.learning_material / "current" / "summary.json"
        summary = json.loads(summary_path.read_text())

        state_path = ctx.workspace.state / "observations.json"
        observations = json.loads(state_path.read_text()) if state_path.exists() else []
        observations.append({"step": summary["step"], "mean_score": summary["mean_score"]})
        state_path.write_text(json.dumps(observations, indent=2) + "\n")

        (ctx.workspace.output / "report.md").write_text("Recorded feedback; adapter unchanged.\n")
        return StepResult(status="done", summary="Recorded feedback; adapter unchanged")
