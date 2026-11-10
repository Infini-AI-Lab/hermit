"""Verify skill projection and deployment receipts with real host files and a scripted OfficeQA acting session."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from hermit.agent.acting.workspace import deploy
from hermit.agent.evolver.methods.coevoskills_adapter import integrity, packaging


ROOT = Path(__file__).resolve().parents[3]
TEMPLATE = ROOT / "hermit/adapter/templates/coevoskills_adapter"


def package(source):
    skill = source / "evo-reader"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: evo-reader\ndescription: Read an input\n---\nUse the reader.\n")
    return skill


def test_every_exported_package_requires_a_manifest(tmp_path):
    source = tmp_path / "exported"
    package(source)
    (source / "evo-incomplete").mkdir()
    with pytest.raises(ValueError, match="evo-incomplete"):
        packaging.project(source, tmp_path / "skills", "q")
    assert not (tmp_path / "skills").exists()


def test_deployed_extensionless_script_reads_the_mapped_input(tmp_path, monkeypatch):
    monkeypatch.delenv("COEVO_TEST_DOCS", raising=False)
    source = tmp_path / "exported"
    skill = package(source)
    script = skill / "read-input"
    script.write_text('#!/bin/sh\ncat "${COEVO_TEST_DOCS:-/root/docs}/sample"\n')
    script.chmod(0o755)
    metadata = '{"separate_path": "/archive/root/docs", "input": "/root/docs"}'
    (skill / "info.json").write_text(metadata)
    blob = b"\x00/root/docs\n"
    (skill / "asset.dat").write_bytes(blob)
    adapter = tmp_path / "adapter"
    shutil.copytree(TEMPLATE, adapter)
    packaging.project(source, adapter / "skills", "q", {"/root/docs": "docs", "/docs": "archive"})
    work = tmp_path / "work"
    deploy(adapter, work, "codex", context="context", serve_tools=False)
    (work / "docs").mkdir()
    (work / "docs/sample").write_text("runtime input")
    installed = work / "hermit/coevoskills-adapter/skills/q-evo-reader"
    result = subprocess.run([installed / "read-input"], cwd=installed.parent.parent, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "runtime input"
    assert json.loads((installed / "info.json").read_text()) == {
        "separate_path": "/archive/root/docs", "input": "../../docs",
    }
    assert (installed / "asset.dat").read_bytes() == blob


@pytest.mark.parametrize("change", ["new adapter file", "execute bit", "missing native installation", "other candidate"])
def test_oracle_verifies_the_deployed_skill_files(tmp_path, change):
    source = tmp_path / "exported"
    package(source)
    adapter = tmp_path / "adapter"
    shutil.copytree(TEMPLATE, adapter)
    packaging.project(source, adapter / "skills", "q")
    work = tmp_path / "work"
    record = deploy(adapter, work, "codex", context="context", serve_tools=False)
    log = tmp_path / "log"
    log.mkdir()
    if change == "new adapter file":
        (work / "hermit/coevoskills-adapter/skills/q-evo-reader/extra.txt").write_text("generated")
    elif change == "execute bit":
        manifest = work / ".codex/skills/q-evo-reader/SKILL.md"
        manifest.chmod(manifest.stat().st_mode | 0o100)
    elif change == "missing native installation":
        record["skills"] = []
        record["skills_dir"] = None
    (log / "deployment.json").write_text(json.dumps(record))
    integrity.record_deployment(work, log)
    if change == "other candidate":
        (adapter / "skills/q-evo-reader/SKILL.md").write_text("Different candidate")
    with pytest.raises(ValueError, match="changed during the run|different candidate"):
        integrity.verify_deployment(adapter, log)


@pytest.mark.parametrize("use_adapter", [False, True])
def test_baseline_and_seed_need_no_receipt(tmp_path, use_adapter):
    work = tmp_path / "work"
    record = deploy(TEMPLATE, work, "codex", context="context", serve_tools=False) if use_adapter else {"mode": "host"}
    log = tmp_path / "log"
    log.mkdir()
    (log / "deployment.json").write_text(json.dumps(record))
    integrity.record_deployment(work, log)
    assert not (log / integrity.RECEIPT).exists()


@pytest.mark.parametrize("sandbox_paths", [False, True])
def test_oracle_accepts_the_public_trainer_log(tmp_path, monkeypatch, sandbox_paths):
    """Run the actual tool, snapshot, CLI adapter deployment, task collector, and scrubbed log publication."""
    pytest.importorskip("harbor")
    from hermit.agent.acting.workspace import run
    from hermit.agent.base import cli
    from hermit.agent.evolver.methods.coevoskills_adapter import oracle
    from hermit.config import Config
    from hermit.sandbox import Bind, Sandbox, inside_tools
    from hermit.tools import ToolServer
    from hermit.trainer.trainer import Trainer
    from toy_task import make_task

    def cli_reply(argv, *, stream, **kwargs):
        Path(argv[argv.index("-o") + 1]).write_text("prediction")
        events = [{"type": "thread.started", "thread_id": "test"},
                  {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}]
        stream.write_text("\n".join(json.dumps(event) for event in events))
        return 0, False, False

    monkeypatch.setattr(cli, "run_process", cli_reply)
    trainer = Trainer(Config.load(make_task(tmp_path, template=str(TEMPLATE))), log=lambda *_: None)
    trainer._prepare()
    collector = ROOT / "examples/officeqa/coevoskills_adapter/oracle_io.py"
    params = {"web_search": False, "workspaces": str(tmp_path / "actor"), "prompt": "Solve the problem",
              "collect": f"{collector}:collect"}
    trainer.pipeline = lambda task, adapter: run(task, adapter, params=params, base=tmp_path)
    source = tmp_path / "exported"
    package(source)
    logs = trainer.run_dir / "agent_logs"
    # Exercise the server's actual path translation on platforms without a Linux process sandbox.
    layout = Sandbox(enabled=sandbox_paths, binds=[Bind(logs, "/w/agent-logs")])
    monkeypatch.setattr(layout, "confines", lambda: sandbox_paths)
    try:
        with ToolServer(inside_tools(trainer._tools(None), layout)) as server:
            monkeypatch.setattr(oracle, "CONFIG", {"seed": str(TEMPLATE), "task_id": "t0", "tool_url": server.url,
                                                  "projection_root": str(trainer.ws.scratch / "probes"),
                                                  "visible_logs": str(logs) if sandbox_paths else None})
            result = oracle.probe(source)
        assert result["output"] == "prediction"
        public = Path(result["log"])
        assert public.is_relative_to(logs) and (public / integrity.RECEIPT).is_file()
        assert not (public.parent / "work").exists()
        record = json.loads((public / "deployment.json").read_text())
        assert not Path(record["adapter"]).exists()  # run_agent released its private snapshot before responding
    finally:
        trainer._close_sandboxes()
