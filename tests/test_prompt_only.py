"""The prompt_only Evolver asks once more when the model's reply holds no complete file."""
import json
from pathlib import Path
import shutil

from harnessadapter.evolver.base import Access, Progress, StepContext, Workspace
from harnessadapter.evolver.loader import find, instantiate

TEMPLATE = Path(__file__).resolve().parents[1] / "harnessadapter" / "algorithms" / "templates" / "prompt_only"


def context(tmp: Path, replies: list[str]) -> tuple[StepContext, list]:
    ws = Workspace(*(tmp / n for n in ("adapter", "material", "info", "history", "state", "scratch", "output")))
    shutil.copytree(TEMPLATE, ws.adapter)
    for d in (ws.task_info, ws.history, ws.state, ws.scratch, ws.output, ws.learning_material / "step_0001" / "tasks"):
        d.mkdir(parents=True, exist_ok=True)
    (ws.learning_material / "current").symlink_to("step_0001")
    (ws.learning_material / "step_0001" / "tasks" / "t1.json").write_text(json.dumps(
        {"id": "t1", "input": {"q": "2+2"}, "output": "5", "judge": {"score": 0.0, "feedback": "expected 4"}}))
    calls = []

    def llm(messages, max_tokens=None, temperature=None):
        calls.append(messages)
        return {"content": replies[len(calls) - 1], "usage": {}}
    tools = {"llm": llm, "check_spec": lambda **_: {"passed": True, "issues": []}}
    return StepContext(workspace=ws, access=Access(), tools=tools, params={"files": ["ADAPTER.md"]},
                       progress=Progress(1, 1, False)), calls


def test_retry_when_the_first_reply_has_no_file(tmp_path):
    evolver, params = instantiate(find("prompt_only"), {})
    ctx, calls = context(tmp_path, ["", "<<<FILE\nAdd carefully.\nFILE>>>\nSUMMARY: added a rule"])
    ctx = StepContext(**{**ctx.__dict__, "params": params})
    result = evolver.step(ctx)
    assert len(calls) == 2 and "did not contain the complete file" in calls[1][-1]["content"]
    assert (ctx.workspace.adapter / "ADAPTER.md").read_text() == "Add carefully.\n"
    assert "added a rule" in result.summary


def test_reply_without_closing_marker_is_used(tmp_path):
    evolver, params = instantiate(find("prompt_only"), {})
    ctx, calls = context(tmp_path, ["<<<FILE\nCheck units.\n\nSUMMARY: added a units rule"])
    ctx = StepContext(**{**ctx.__dict__, "params": params})
    evolver.step(ctx)
    assert len(calls) == 1 and (ctx.workspace.adapter / "ADAPTER.md").read_text() == "Check units.\n"
    assert (ctx.workspace.output / "llm_replies" / "ADAPTER.md.0.txt").exists()
