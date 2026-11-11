"""The stand-in `pi` the methods' Pi tests run (tests/methods/*/test_*_on_pi.py): a script on PATH that replays a stream
the real CLI printed (tests/fixtures/pi), makes the edits a test names and records what each call was given. No test
reaches a real host or needs the real CLI."""
import json
import os
from pathlib import Path
import stat
import sys

import pytest

from hermit.agent.model.backends import bind_agent_params, registry

ROOT = Path(__file__).resolve().parents[1]
STREAMS = ROOT / "tests" / "fixtures" / "pi"

SCRIPT = f'''#!{sys.executable}
"""A stand-in for `pi --mode json ...`: records its arguments, working directory, home files, extensions, the keys it
was given and stdin, makes the edits in edits.json (relative to its working directory), then prints stream.jsonl."""
import json, os, sys
from pathlib import Path
here = Path(__file__).resolve().parent
home = Path(os.environ["PI_CODING_AGENT_DIR"])
seen = {{"args": sys.argv[1:], "cwd": os.getcwd(), "stdin": sys.stdin.read(),
         "files": {{p.name: p.read_text() for p in sorted(home.glob("*.json")) if p.is_file()}},
         "keys": sorted(k for k in os.environ if k.endswith("_KEY")),
         "extensions": sorted(p.name for p in home.glob("extensions/*"))}}
with open(here / "seen.jsonl", "a") as f:
    f.write(json.dumps(seen) + "\\n")
for name, text in json.loads((here / "edits.json").read_text()).items():
    Path(name).parent.mkdir(parents=True, exist_ok=True)
    Path(name).write_text(text)
sys.stdout.write((here / "stream.jsonl").read_text())
'''


class Stand:
    def __init__(self, folder: Path):
        self.folder = folder

    def play(self, stream: str = "resume", reply: str | None = None, edits: dict | None = None, cut: bool = False,
             stopped: str | None = None) -> None:
        """The next calls replay `stream` (a file of tests/fixtures/pi), `reply` as the final answer, and make `edits`;
        with `cut`, the last reply ends at its output limit; with `stopped`, the provider stopped it after its text with
        that finish reason (`content_filter`, a refusal, or `insufficient_system_resource`), which Pi 1.5.0 reports as
        an error, sent once. `resume` has no tool calls, `success` has three."""
        lines = (STREAMS / f"{stream}.jsonl").read_text().splitlines(keepends=True)
        last = max(i for i, line in enumerate(lines) if '"type":"message_end"' in line and '"assistant"' in line)
        if cut:
            lines[last] = lines[last].replace('"stopReason":"stop"', '"stopReason":"length"')
        if stopped:
            event = json.loads(lines[last])
            event["message"].update(stopReason="error", rawStopReason=stopped,
                                    errorMessage=f"Provider finish_reason: {stopped}")
            lines[last] = json.dumps(event, separators=(",", ":")) + "\n"
        text = "".join(lines)
        (self.folder / "stream.jsonl").write_text(
            text if reply is None else text.replace("the answer", json.dumps(reply)[1:-1]))
        (self.folder / "edits.json").write_text(json.dumps(edits or {}))

    def seen(self) -> list[dict]:
        """What each call so far was given, and forget them."""
        path = self.folder / "seen.jsonl"
        out = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        path.unlink(missing_ok=True)
        return out


@pytest.fixture
def pi_stand(tmp_path, monkeypatch):
    folder = tmp_path / "pi-bin"
    folder.mkdir()
    exe = folder / "pi"
    exe.write_text(SCRIPT)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_KEY", "sk-fake")
    stand = Stand(folder)
    stand.play()
    return stand


def bound_params(key_env: str = "FAKE_KEY", **more) -> dict:
    """A learner's params with its model bound for Pi (`fake/m-1`, its key in `key_env`), as the Evolver's loader
    binds them."""
    backends = registry({"fake": {"base_url": "http://127.0.0.1:9/v1", "api_key_env": key_env, "apis": ["chat"],
                                  "prices": {"m-1": {"input": 1.0, "cached_input": 0.1, "output": 2.0}}}})
    return {**bind_agent_params({"model": "fake/m-1", "backend": "pi"}, "pi", backends), "web_search": False, **more}
