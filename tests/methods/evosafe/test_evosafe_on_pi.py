"""EvoSafeHarness on Pi (`evolver.base: pi`): its per-role sessions against the stand-in `pi` (tests/pi_stand.py), its
dispatch of the proposer and the roles to them, and a whole training whose proposer is a stand-in for that role."""
import json
from pathlib import Path
from types import SimpleNamespace as NS

import yaml

from pi_stand import bound_params, pi_stand  # noqa: F401 (pi_stand: the fixture)

TEMPLATE = Path(__file__).resolve().parents[3] / "hermit" / "adapter" / "templates" / "evosafe"


def ctx_of(tmp_path):
    ws = NS(scratch=tmp_path / "scratch", output=tmp_path / "out")
    ws.scratch.mkdir()
    ws.output.mkdir()
    return NS(workspace=ws, params=bound_params(), sandbox=None, cancel=None, tools={})


def test_a_role_on_pi_has_none_of_its_own_tools_and_answers_the_schema(pi_stand, tmp_path):  # noqa: F811
    from hermit.agent.evolver.methods.evosafe import cli_roles, pi_agent
    assert cli_roles.proposer({"backend": "pi"}) == "pi"
    pi_stand.play(reply='{"verdict": "ok"}')
    messages = [{"role": "system", "content": "Be exact."}, {"role": "user", "content": "the evidence"}]
    schema = {"type": "object", "properties": {"verdict": {"type": "string"}}, "required": ["verdict"]}
    response, usage, error = pi_agent.call_role(ctx_of(tmp_path), messages, schema, "trial-001-designer")
    [call] = pi_stand.seen()
    assert error is None and json.loads(response["content"]) == {"verdict": "ok"} and usage[0].calls == 1
    assert "--no-builtin-tools" in call["args"] and "--no-session" in call["args"]
    assert call["args"][:6] == ["--mode", "json", "--provider", "hermit", "--model", "m-1"]
    assert call["args"][call["args"].index("--append-system-prompt") + 1] == "Be exact."
    assert "the evidence" in call["stdin"] and "satisfies this JSON schema" in call["stdin"]


def test_a_reply_that_is_no_answer_fails_the_role(pi_stand, tmp_path):  # noqa: F811
    from hermit.agent.evolver.methods.evosafe import pi_agent
    response, _, error = pi_agent.call_role(ctx_of(tmp_path), [{"role": "user", "content": "x"}],
                                            {"type": "object", "required": ["verdict"]}, "trial-002-designer")
    assert response is None and error.startswith("pi role failed")


def test_a_legacy_proposer_pi_without_a_model_is_refused_with_how_to_give_it_one(tmp_path):
    from hermit.agent.evolver.methods.evosafe.evolver import EvoSafeEvolver
    ctx = NS(workspace=NS(), params={"proposer": "pi", "eval_task_ids": ["a", "b"]}, access=NS(validation=None), tools={})
    result = EvoSafeEvolver().step(ctx)
    assert result.status == "failed" and "evolver.base: pi and evolver.model" in result.summary


def test_the_roles_go_to_pi_when_it_is_the_proposer(tmp_path, monkeypatch):
    from hermit.agent.evolver.methods.evosafe import pi_agent, roles
    asked = []

    def call(ctx, messages, schema, name):
        asked.append(name)
        return {"content": json.dumps({"verdict": "ok"})}, [], None
    monkeypatch.setattr(pi_agent, "call_role", call)
    ws = NS(output=tmp_path, state=tmp_path)
    ctx = NS(workspace=ws, params={"backend": "pi"}, cancel=None, tools={})
    schema = {"type": "object", "required": ["verdict"], "properties": {"verdict": {"type": "string"}}}
    assert roles.invoke_role(ctx, "Review it.", {"case": 1}, schema, "trial-001-criticizer", []) == {"verdict": "ok"}
    assert asked == ["trial-001-criticizer"]


def test_a_whole_training_proposes_on_pi_with_its_bound_model(tmp_path, monkeypatch):
    """From `evolver.base: pi` through the Trainer: EvoSafe keeps its own proposer (it names one), bound to the model,
    and its proposal is the Pi role's answer."""
    from hermit.agent.evolver.methods.evosafe import pi_agent
    from hermit.config import Config
    from hermit.trainer.trainer import Trainer
    monkeypatch.setenv("FAKE_KEY", "sk-fake")
    (tmp_path / "pipeline.py").write_text('''
from hermit.task import RunOutput
def run(task, adapter):
    improved = "tested revision" in (adapter / "ADAPTER.md").read_text()
    return RunOutput(output={"kind": task["kind"], "utility": True,
                             "attack_success": None if task["kind"] == "clean" else not improved}, cost_usd=0.0, tokens=0)
''')
    (tmp_path / "judge.py").write_text("from hermit.task import JudgeResult\n\ndef score(task, output):\n"
                                       "    return JudgeResult(score=1.0, extra={\"metrics\": output.output})\n")
    rows = [{"id": f"{s}-{k}", "kind": k, **({"injection_task_id": "f"} if k == "injected" else {})}
            for s in ("train", "val") for k in ("clean", "injected")]
    (tmp_path / "tasks.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    for split in ("train", "val"):
        (tmp_path / f"{split}.txt").write_text(f"{split}-clean\n{split}-injected\n")
    asked = []

    def call(ctx, messages, schema, name):
        asked.append(ctx.params)
        files = {n: (ctx.workspace.adapter / n).read_text() for n in ("ADAPTER.md", "defense.py", "BUNDLE.md")}
        files["ADAPTER.md"] += "\ntested revision\n"
        return {"content": json.dumps({"summary": "on pi", "files": files})}, [], None
    monkeypatch.setattr(pi_agent, "call_role", call)
    config = {"data": {"tasks": "tasks.jsonl", "train": "train.txt", "val": "val.txt", "step_size": 2},
              "template": str(TEMPLATE), "agent": {"name": "pipeline.py:run", "concurrency": 1},
              "judge": {"path": "judge.py"}, "sandbox": {"enabled": False},
              "backends": {"fake": {"base_url": "http://127.0.0.1:9/v1", "api_key_env": "FAKE_KEY", "apis": ["chat"]}},
              "evolver": {"name": "evosafe", "base": "pi", "model": "fake/m-1"},
              "trainer": {"run_name": "on-pi", "output_dir": "runs", "epochs": 1}}
    (tmp_path / "run.yaml").write_text(yaml.safe_dump(config))
    trainer = Trainer(Config.load(tmp_path / "run.yaml"), log=lambda *_: None)
    trainer.train()
    assert "tested revision" in (trainer.ws.adapter / "ADAPTER.md").read_text()
    assert asked and all(p["backend"] == "pi" and p["provider"]["apiKey"] == "$FAKE_KEY" for p in asked)
