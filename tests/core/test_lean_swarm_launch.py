"""The Lean swarm launcher (examples/lean_swarm/launch.py): what its run directory keeps."""
import importlib.util
from pathlib import Path

LAUNCH = Path(__file__).resolve().parents[2] / "examples" / "lean_swarm" / "launch.py"


def _launch():
    spec = importlib.util.spec_from_file_location("lean_swarm_launch", LAUNCH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_run_directory_keeps_no_key_value(tmp_path, monkeypatch):
    """An agent's codex home snapshots its environment, the model's key with it; the run is scrubbed at its end,
    also when the key came from a key file and not from this process's environment."""
    key = "sk-lean-swarm-0123456789abcdef"
    monkeypatch.setenv("LEAN_SWARM_MODEL_API_KEY", "placeholder")
    monkeypatch.delenv("LEAN_SWARM_MODEL_API_KEY")
    monkeypatch.delenv("MODEL_CREDENTIAL", raising=False)
    snapshot = tmp_path / ".codex_homes" / "A1" / "shell_snapshots" / "s.sh"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text(f"export MODEL_CREDENTIAL={key}\n")       # a key variable of any name
    _launch().scrub_run(tmp_path, key)
    assert snapshot.read_text() == "export MODEL_CREDENTIAL=<LEAN_SWARM_MODEL_API_KEY>\n"


def test_a_key_file_and_a_different_key_in_the_environment_are_both_scrubbed(tmp_path, monkeypatch):
    env_key, file_key = "sk-lean-env-0123456789abcdef", "sk-lean-file-0123456789abcdef"
    monkeypatch.setenv("LEANTEST2_API_KEY", env_key)
    monkeypatch.setenv("LEAN_SWARM_MODEL_API_KEY", "placeholder")
    monkeypatch.delenv("LEAN_SWARM_MODEL_API_KEY")
    log = tmp_path / "relay.log"
    log.write_text(f"{env_key} {file_key}\n")
    _launch().scrub_run(tmp_path, file_key)
    assert log.read_text() == "<LEANTEST2_API_KEY> <LEAN_SWARM_MODEL_API_KEY>\n"


def test_a_stopping_run_stops_its_compiles_and_starts_none(tmp_path):
    """On SIGTERM, before the scrub: the judge's running compiles are stopped and reaped, and none starts after."""
    import subprocess
    launch = _launch()
    running = subprocess.Popen(["sleep", "60"], start_new_session=True)
    launch.COMPILES.add(running)
    launch.STOPPING.set()
    launch.stop_compiles()
    assert running.poll() is not None
    rc, out = launch.Judge("", "t").compile(tmp_path / "Grade.lean")
    assert rc == 1 and "the run is stopping" in out


def test_stopping_the_compiles_takes_no_lock(tmp_path):
    """The SIGTERM handler runs in the main thread, which it may have interrupted holding COMPILES_LOCK (a compile
    started there): stopping the compiles must not wait for that lock."""
    import threading
    launch = _launch()
    launch.STOPPING.set()
    done = threading.Event()
    with launch.COMPILES_LOCK:
        worker = threading.Thread(target=lambda: (launch.stop_compiles(), done.set()), daemon=True)
        worker.start()
        assert done.wait(10)


def test_a_jail_imports_hermit_from_the_path_not_its_working_directory():
    """`python -m` puts its working directory first: a jail started in another checkout would import that one's
    hermit. -P leaves it out; this checkout comes from PYTHONPATH, which main() sets."""
    prefix = _launch()._jail({"network": "no-network"})
    assert prefix[1:4] == ["-P", "-m", "hermit.sandbox"] and prefix[-1] == "--"
