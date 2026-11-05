"""The rollout's log: a curated copy of what the trial (or the step) left, without the agent's raw session
transcripts.

    hermit-log/
      result.json              the trial's or step's result as Harbor wrote it
      exception.txt            the exception type, message and traceback, when there was one
      harbor.log, job.yaml     the job's own log and config (a trial; a stream's step has the driver's log)
      agent/                   the agent's text outputs (codex.txt, ...), trajectory.json (ATIF), *.jsonl, task.md,
                               hook-state/; never sessions/
      verifier/                test-stdout.txt (its last 20,000 characters), reward.*
"""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import shutil

STDOUT_TAIL = 20_000
AGENT_SKIP = {"sessions"}                                # directories of the agent's logs never copied
AGENT_FILE_LIMIT = 50 * 1024 * 1024                      # a file larger than this is named, not copied


def seconds(timing: dict | None) -> float | None:
    """A Harbor TimingInfo's duration in seconds."""
    if not timing or not timing.get("started_at") or not timing.get("finished_at"):
        return None
    stamps = [datetime.fromisoformat(str(timing[k]).replace("Z", "+00:00")) for k in ("started_at", "finished_at")]
    return round((stamps[1] - stamps[0]).total_seconds(), 1)


def model_calls(trajectory: Path) -> int:
    """The ATIF steps carrying token metrics: one per model call."""
    try:
        steps = json.loads(trajectory.read_text(encoding="utf-8")).get("steps") or []
    except (OSError, ValueError, AttributeError):
        return 0
    return sum(1 for s in steps if isinstance(s, dict) and s.get("metrics"))


def curate(outputs: Path | None, job: Path, verdict: dict, *, result: dict | None = None,
           extra_files: dict[str, Path] | None = None) -> Path:
    """The curated log directory under `job`: the result and the agent's and verifier's outputs from `outputs`
    (a trial directory, or a step's directory holding agent/ and verifier/), the verdict's exception, and
    `extra_files` ({name: path}: the job's harbor.log, the stream's driver.log)."""
    log = job / "hermit-log"
    shutil.rmtree(log, ignore_errors=True)
    (log / "agent").mkdir(parents=True)
    (log / "verifier").mkdir()
    for name, path in {"job.yaml": job / "job.yaml", "harbor.log": job / "harbor.log", **(extra_files or {})}.items():
        if path.is_file():
            shutil.copy2(path, log / name)
    if verdict.get("exception_type") or verdict.get("exception_message"):
        (log / "exception.txt").write_text(f"{verdict.get('exception_type') or verdict['failure_cause']}: "
                                           f"{verdict.get('exception_message', '')}\n", encoding="utf-8")
    if result is not None:
        (log / "result.json").write_text(json.dumps(result, indent=1, default=str) + "\n", encoding="utf-8")
        trace = ((result.get("exception_info") or {}).get("exception_traceback"))
        if trace:
            with open(log / "exception.txt", "a", encoding="utf-8") as f:
                f.write("\n" + str(trace))
    if outputs is None:
        return log
    if result is None and (outputs / "result.json").is_file():
        shutil.copy2(outputs / "result.json", log / "result.json")
    agent = outputs / "agent"
    if agent.is_dir():
        for child in sorted(agent.iterdir()):
            if child.is_dir():
                if child.name in AGENT_SKIP:
                    continue
                if child.name in ("hook-state", ".hermit"):
                    shutil.copytree(child, log / "agent" / child.name, dirs_exist_ok=True)
                continue
            if child.suffix in (".txt", ".json", ".jsonl", ".md", ".log") and child.stat().st_size <= AGENT_FILE_LIMIT:
                shutil.copy2(child, log / "agent" / child.name)
    verifier = outputs / "verifier"
    stdout = verifier / "test-stdout.txt"
    if stdout.is_file():
        text = stdout.read_text(encoding="utf-8", errors="replace")
        if len(text) > STDOUT_TAIL:
            text = f"[... {len(text) - STDOUT_TAIL} characters dropped ...]\n" + text[-STDOUT_TAIL:]
        (log / "verifier" / "test-stdout.txt").write_text(text, encoding="utf-8")
    for reward in verifier.glob("reward.*") if verifier.is_dir() else []:
        shutil.copy2(reward, log / "verifier" / reward.name)
    return log
