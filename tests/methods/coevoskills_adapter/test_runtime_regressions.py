"""Regression checks for package transport, worker configuration, and transactional skill updates."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hermit.agent.evolver.methods.coevoskills_adapter.evolver import CoEvoSkillsAdapterEvolver, worker_env


def test_worker_gets_docker_configuration_and_uses_only_the_trainer_for_its_model(monkeypatch):
    monkeypatch.setenv("DOCKER_CONTEXT", "local-development")
    monkeypatch.setenv("OPENAI_API_KEY", "example")
    env = worker_env()
    assert env["DOCKER_CONTEXT"] == "local-development"
    assert "OPENAI_API_KEY" not in env
    assert env["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"
    assert (Path(env["PYTHONPATH"]) / "hermit").is_dir()


def test_container_cleanup_reports_daemon_failures(monkeypatch):
    from hermit.agent.evolver.methods.coevoskills_adapter import evolver
    monkeypatch.setattr(shutil, "which", lambda _: "/bin/docker")

    def docker(args, **kwargs):
        error = "daemon unavailable" if args[1] == "ps" else "No such image: hb__q"
        return subprocess.CompletedProcess(args, 1, "", error)
    monkeypatch.setattr(subprocess, "run", docker)
    assert evolver.remove_containers("trial-q", "q") == "docker ps failed: daemon unavailable"


def make_skill(root, name, contents):
    package = root / name
    (package / "references").mkdir(parents=True)
    (package / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Example\n---\nRead the guide.\n")
    (package / "references" / "guide.txt").write_text(contents)


def test_complete_upstream_host_export_and_rollback(tmp_path, monkeypatch):
    pytest.importorskip("harbor")
    if not os.environ.get("COEVOSKILLS_ROOT"):
        pytest.skip("requires the pinned CoEvoSkills checkout")
    from hermit.agent.evolver.methods.coevoskills_adapter.upstream import checkout
    checkout()
    from hermit.agent.evolver.methods.coevoskills_adapter.evolution_agent import HermitEvolutionAgent

    class TaskEnvironment:
        environment_dir = tmp_path / "task" / "environment"

        async def download_dir(self, source, destination):
            name = Path(source).name
            shutil.copytree(tmp_path / "container" / name, Path(destination) / name)

        async def exec(self, command, timeout_sec):
            return SimpleNamespace(return_code=0)

        async def upload_dir(self, source, destination):
            shutil.copytree(source, tmp_path / "oracle" / Path(destination).name)

    env = TaskEnvironment()
    agent = object.__new__(HermitEvolutionAgent)
    agent.hermit_export = tmp_path / "selected"
    host = env.environment_dir / "skills"
    make_skill(host, "evo-old", "from the task before evolution")
    make_skill(tmp_path / "container", "evo-first", "round one")
    make_skill(tmp_path / "container", "evo-second", "round two")
    skill = tmp_path / "container/evo-first"
    (skill / "references/lookup.bin").write_bytes(bytes([0, 255, 83]))
    script = skill / "scripts/nested/run"
    script.parent.mkdir(parents=True)
    script.write_bytes(b"#!/bin/sh\nexit 0\n")
    script.chmod(0o751)

    names = {"evo-first", "evo-second"}
    monkeypatch.setattr(agent, "_list_container_skills", AsyncMock(return_value=names))
    monkeypatch.setattr(agent, "_validate_evolved_skill_schema", AsyncMock(return_value=[]))
    # Exercise both of the pinned caller's normal paths: the intermediate snapshot sent to the oracle
    # and the selected export stored after the run.
    oracle = asyncio.run(agent._export_evolved_skills_to_host(env))
    try:
        asyncio.run(agent._inject_skill_dir_to_container("evo-first", oracle / "evo-first", env))
    finally:
        shutil.rmtree(oracle)
    asyncio.run(agent._import_agent_created_skills(env, set()))
    assert {p.name for p in agent.hermit_export.iterdir()} == names
    assert (agent.hermit_export / "evo-second/references/guide.txt").read_text() == "round two"
    for base in (agent.hermit_export / "evo-first", tmp_path / "oracle/evo-first"):
        assert (base / "references/lookup.bin").read_bytes() == bytes([0, 255, 83])
        assert (base / "scripts/nested/run").read_bytes() == script.read_bytes()
        assert (base / "scripts/nested/run").stat().st_mode & 0o777 == 0o751

    best = tmp_path / "oracle-snapshot"
    make_skill(best, "evo-first", "best verified version")
    agent._rollback_host_skills(env, best)
    assert [p.name for p in agent.hermit_export.iterdir()] == ["evo-first"]
    assert (agent.hermit_export / "evo-first/references/guide.txt").read_text() == "best verified version"


@pytest.mark.parametrize("failure", ["bank check", "owner record"])
def test_failed_bank_update_preserves_old_packages_and_owner_record(tmp_path, monkeypatch, failure):
    for path in ("adapter", "scratch", "state", "output/q"):
        (tmp_path / path).mkdir(parents=True)
    ws = SimpleNamespace(**{key: tmp_path / key for key in ("adapter", "scratch", "state", "output")})
    ctx = SimpleNamespace(workspace=ws, tools={}, params={})
    make_skill(tmp_path / "first", "evo-first", "old")
    make_skill(tmp_path / "next", "evo-next", "replacement")
    CoEvoSkillsAdapterEvolver._install(ctx, "q", tmp_path / "first")
    record = ws.state / "installed.json"
    previous = record.read_bytes()
    if failure == "bank check":
        def check(path):
            assert not list(Path(path).glob(".candidate-*"))
            return {"passed": False, "issues": [{"path": "", "problem": "could not validate the complete adapter"}]}
        ctx.tools["check_spec"] = check
    else:
        replace = Path.replace

        def full_disk(self, target):
            if target == record:
                raise OSError("owner record could not be committed")
            return replace(self, target)
        monkeypatch.setattr(Path, "replace", full_disk)
    with pytest.raises((OSError, ValueError), match="(owner record|complete adapter)"):
        CoEvoSkillsAdapterEvolver._install(ctx, "q", tmp_path / "next")
    assert record.read_bytes() == previous
    assert json.loads(previous) == {"q": ["q-evo-first"]}
    assert [p.name for p in (ws.adapter / "skills").iterdir()] == ["q-evo-first"]
    assert (ws.adapter / "skills/q-evo-first/references/guide.txt").read_text() == "old"
