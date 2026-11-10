"""The RRSI Adapter's roles on Pi (`evolver.base: pi`), against the stand-in `pi` (tests/pi_stand.py): what keeps a role
read-only on the other CLIs, Pi's own way."""
import json
from pathlib import Path

import pytest

from claude_stand import roles_ctx
from pi_stand import bound_params, pi_stand  # noqa: F401 (pi_stand: the fixture)


def roles_of(tmp_path, **more):
    from hermit.agent.evolver.methods.rrsi_adapter import roles as R
    from hermit.agent.evolver.methods.rrsi_adapter.evolver import RRSIAdapterEvolver
    ctx = roles_ctx(tmp_path, {"login": None, **bound_params(), **more})
    evolver = RRSIAdapterEvolver()
    evolver.directory = Path(R.__file__).parent
    return evolver._roles(ctx)


def test_a_role_on_pi_has_no_tools_a_digester_only_those_that_read_and_each_its_bound_model(pi_stand, tmp_path):  # noqa: F811
    pytest.importorskip("rrsi")
    roles = roles_of(tmp_path)
    assert roles.backend == "pi"
    assert roles.generate("look at this", system="You are the analyst.") == "the answer"
    [analyst] = pi_stand.seen()
    assert {"--no-tools", "--no-mcp", "--no-context-files"} <= set(analyst["args"])
    assert analyst["args"][:6] == ["--mode", "json", "--provider", "hermit", "--model", "m-1"]
    assert analyst["args"][analyst["args"].index("--append-system-prompt") + 1] == "You are the analyst."
    assert analyst["stdin"] == "look at this"
    assert json.loads(analyst["files"]["models.json"])["providers"]["hermit"]["apiKey"] == "$FAKE_KEY"
    roles.traces = tmp_path / "traces"
    roles.traces.mkdir()
    assert roles.generate("read it", system="You are a trajectory digester.") == "the answer"
    [digester] = pi_stand.seen()
    assert digester["args"][digester["args"].index("--tools") + 1] == "read,grep,find,ls"
    assert "--no-tools" not in digester["args"] and digester["cwd"] == str(roles.traces.resolve())


def test_the_roles_own_binding_is_their_provider_on_pi(pi_stand, tmp_path):  # noqa: F811
    pytest.importorskip("rrsi")
    from hermit.agent.model.backends import Binding, as_binding, registry
    backends = registry({"other": {"base_url": "http://127.0.0.1:8/v1", "api_key_env": "OTHER_KEY", "apis": ["chat"]}})
    binding: Binding = as_binding("other/m-2", backends)
    roles = roles_of(tmp_path, roles={"model": "m-2", "binding": binding.spec()})
    assert roles.settings["provider"]["baseUrl"] == "http://127.0.0.1:8/v1" and roles.settings["model"] == "m-2"
    assert roles.settings["provider"]["apiKey"] == "$OTHER_KEY"
