"""The toy task the Trainer tests run on (two numbers to add), shared by the tests of every layer."""
import json
from pathlib import Path

import yaml

TEMPLATE = Path(__file__).resolve().parents[1] / "hermit" / "adapter" / "templates" / "prompt_only"

PIPELINE = '''
from pathlib import Path
from hermit.task import RunOutput

def run(task, adapter):
    text = (Path(adapter) / "ADAPTER.md").read_text() if adapter else ""
    answer = str(task["a"] + task["b"]) if "add the numbers" in text else "?"
    log = Path(__file__).parent / ".logs" / f"{task['id']}.txt"
    log.parent.mkdir(exist_ok=True)
    log.write_text(f"question {task['a']}+{task['b']} answer {answer}")
    return RunOutput(output=answer, log=log, cost_usd=0.01, wall_s=0.1, api_speed={"tps": 50}, tokens=100)
'''
JUDGE = '''
def score(task, output):
    ok = output.output == str(task["a"] + task["b"])
    return {"score": float(ok), "feedback": "correct" if ok else f"expected the sum of {task['a']} and {task['b']}"}
'''
EVOLVER = '''
from hermit.agent.evolver.base import Evolver, StepResult

class Toy(Evolver):
    def step(self, ctx):
        mode = ctx.params.get("mode", "learn")
        tasks = list((ctx.workspace.learning_material / "current" / "tasks").glob("*.json"))
        assert tasks, "material must exist"
        if mode == "learn":
            (ctx.workspace.adapter / "ADAPTER.md").write_text("Always add the numbers.\\n")
        elif mode == "break":
            (ctx.workspace.adapter / ".hermit" / "EVOLVER.md").write_text("changed the metadata\\n")
            (ctx.workspace.adapter / "stray.txt").write_text("outside every learnable path\\n")
            (ctx.workspace.adapter / "ADAPTER.md").write_text("Always add the numbers.\\n")
        elif mode == "stop":
            return StepResult("stop", "converged")
        elif mode == "spend":                       # spends, and is stopped by the Trainer's budget
            from hermit.agent.evolver.base import Usage
            ctx.cancel.wait(30)
            return StepResult("failed", "stopped", usage=[Usage(model="m", input_tokens=1234, output_tokens=0,
                                                                calls=1, cost_usd=0.5)])
        elif mode == "probe":                        # runs a training task through the run_agent tool
            import json as _json
            out = ctx.tools["run_agent"](task_ids=["t0"])
            first = out["results"][0]
            first["log_text"] = open(first["log"]).read() if first.get("log") else None   # readable here
            (ctx.workspace.output / "probe.json").write_text(_json.dumps(first, default=str))
        (ctx.workspace.state / "seen.txt").write_text(str(ctx.progress.step))
        (ctx.workspace.output / "report.md").write_text(f"step {ctx.progress.step}: {mode}\\n")
        return StepResult("done", f"{mode} at step {ctx.progress.step}")
'''


def make_task(tmp: Path, **overrides) -> Path:
    (tmp / "task-info").mkdir()
    (tmp / "task-info" / "README.md").write_text("Add two numbers.\n")
    rows = [{"id": f"t{i}", "a": i, "b": 2 * i} for i in range(10)]
    (tmp / "tasks.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (tmp / "train.txt").write_text("\n".join(f"t{i}" for i in range(6)) + "\n")
    (tmp / "val.txt").write_text("t6\n")
    (tmp / "test.txt").write_text("t7\nt8\nt9\n")
    (tmp / "pipeline.py").write_text(PIPELINE)
    (tmp / "judge.py").write_text(JUDGE)
    (tmp / "toy").mkdir()
    (tmp / "toy" / "evolver.py").write_text(EVOLVER)
    (tmp / "toy" / "evolver.yaml").write_text("class: evolver.py:Toy\nneeds: []\nparams: {mode: learn}\n")
    config = {"data": {"tasks": "tasks.jsonl", "train": "train.txt", "val": "val.txt", "test": "test.txt",
                       "step_size": 4},
              "task_info": "task-info", "agent": {"name": "pipeline.py:run", "concurrency": 2},
              "judge": {"path": "judge.py"}, "template": str(TEMPLATE), "evolver": {"name": "toy"},
              "sandbox": {"enabled": False},
              "trainer": {"run_name": "toy-run", "output_dir": "runs", "epochs": 2,
                          "save_freq": {"steps": 1, "epochs": 1}, "budget": {"usd": 5}}}
    for key, value in overrides.items():
        section, _, sub = key.partition("__")
        if sub:
            config.setdefault(section, {})[sub] = value
        else:
            config[section] = value
    path = tmp / "run.yaml"
    path.write_text(yaml.safe_dump(config))
    return path


def make_sandboxed_task(tmp: Path, **overrides) -> Path:
    """The toy task under the run's sandbox: its agent's code in agent/ (the task sandbox's one declared path), the
    data, the judge and the runs beside it, never shown to the agent."""
    path = make_task(tmp, **{"sandbox": {"enabled": True}, "agent": {"name": "agent/pipeline.py:run", "concurrency": 2},
                             "environment": {"read": ["agent"]}, **overrides})
    (tmp / "agent").mkdir(exist_ok=True)
    (tmp / "agent" / "pipeline.py").write_text(PIPELINE)
    return path
