"""MWM's learner on Pi (`evolver.base: pi`): a whole training against a stand-in `pi` that does what an MWM session does,
its stream the one the real CLI printed (tests/fixtures/pi)."""
import json
import shutil

import pytest
import yaml

from hermit.config import Config

NEEDS_BWRAP = pytest.mark.skipif(not shutil.which("bwrap"), reason="needs bubblewrap")
# The sandbox forwards a loopback model's port into the session's network, where only root may listen below 1024
BACKENDS = {"fake": {"base_url": "http://127.0.0.1:59999/v1", "api_key_env": "FAKE_KEY", "apis": ["chat"],
                     "prices": {"m-1": {"input": 1.0, "cached_input": 0.1, "output": 2.0}}}}

FAKE_MWM_PI = r'''#!/usr/bin/env python3
"""The MWM learner on Pi: it writes a note in its working directory, the world model, records what it was given, and
prints the stream the real Pi printed (three tool calls)."""
import json, os, sys
from pathlib import Path
home = Path(os.environ["PI_CODING_AGENT_DIR"])
n = len(list(Path("memory").glob("note*.md")))
Path("memory", f"note{n}.md").write_text(
    f"last_confirmed_t: {n}\nscope: always\nderived_from: none (reasoning only)\nbrief: note {n}\n\nA fact.\n")
Path("meta", "pi.json").write_text(json.dumps({"args": sys.argv[1:-1], "cwd": os.getcwd(),
                                               "models": (home / "models.json").read_text(),
                                               "extensions": sorted(p.name for p in home.glob("extensions/*"))}))
sys.stdout.write(Path(__file__).with_name("stream.jsonl").read_text())
'''


@NEEDS_BWRAP
def test_the_mwm_learner_runs_on_pi_with_its_bound_model(tmp_path, monkeypatch):
    """From `evolver.base: pi` through the Trainer: each session is told its bound model (Pi's provider, written from the
    model reference), works in /w, and is adopted by what the standard steps say it did."""
    import test_mwm_evolver as M
    from hermit.trainer.trainer import Trainer
    from pi_stand import STREAMS
    monkeypatch.setenv("FAKE_KEY", "sk-fake")
    path = M.make(tmp_path, monkeypatch)
    data = yaml.safe_load(path.read_text())
    data["evolver"].update(base="pi", model="fake/m-1", pi={"command_timeout_s": 0})
    data["backends"] = BACKENDS
    path.write_text(yaml.safe_dump(data))
    fake = tmp_path / "fakebin" / "pi"
    fake.write_text(FAKE_MWM_PI)
    fake.chmod(0o755)
    shutil.copy(STREAMS / "success.jsonl", fake.with_name("stream.jsonl"))
    Trainer(Config.load(path), log=lambda *_: None).train()
    run = tmp_path / "runs" / "mwm-test"
    steps = [json.loads(line) for line in (run / "steps.jsonl").read_text().splitlines()]
    assert [s["evolver"]["status"] for s in steps] == ["done", "done", "done"], \
        [s["evolver"].get("summary") for s in steps]
    sessions = json.loads((run / "step_0001" / "output" / "sessions.json").read_text())
    assert sessions[0]["status"] == "adopted" and sessions[0]["tool_calls"] == 3 and sessions[0]["turns"] == 4
    assert (run / "step_0001" / "output" / "review_0001.pi.jsonl").is_file()
    saw = json.loads((run / "checkout" / "meta" / "pi.json").read_text())
    assert saw["cwd"] == "/w" and saw["args"][:6] == ["--mode", "json", "--provider", "hermit", "--model", "m-1"]
    assert json.loads(saw["models"])["providers"]["hermit"]["apiKey"] == "$FAKE_KEY"
    assert saw["extensions"] == []                                  # evolver.pi's command_timeout_s 0, passed on
