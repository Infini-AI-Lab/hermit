"""EvoSafeHarness's per-role sessions on Pi (`evolver.base: pi`), against the stand-in `pi` (tests/pi_stand.py)."""
import json
from types import SimpleNamespace as NS

from pi_stand import bound_params, pi_stand  # noqa: F401 (pi_stand: the fixture)


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
