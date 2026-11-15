"""The rollout's log: a curated copy of what the trial (or the step) left, without the agent's raw session
transcripts, the CLI's home or a sign-in.

    hermit-log/
      result.json              the trial's or step's result as Harbor wrote it
      exception.txt            the exception type, message and traceback, when there was one
      harbor.log, job.yaml     the job's own log and config (a trial; a stream's step has the driver's log)
      agent/                   the agent's outputs: the CLI's stream (agent-<backend>.jsonl), the answer agent's
                               answer.json, Harbor's own agents' text outputs and trajectory.json (ATIF), task.md,
                               hook-state/, hermit-collect/ (the CLI's files brought out: its last message); never
                               sessions/ (a CLI's session logs, collected ones included), nor a binary file of the
                               CLI's home (OpenCode's database)
      memory/                  the notes the deployed adapter kept (hermit/<id>/memory, copied back after the session:
                               Continual Harness reads memory/NOTES.md)
      tool-calls.jsonl         each call of the adapter's tools served for the trial (hermit.harbor.tools)
      verifier/                test-stdout.txt (its last 20,000 characters, with `verifier_redact`'s patterns
                               removed), reward.*

Every file kept is scrubbed of this process's key values (hermit.redact). The job's `launch/` (the CLI's home, the
sign-in that came back from the container) is never copied, and nothing is read through a link the container left.
"""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import stat

STDOUT_TAIL = 20_000
AGENT_SKIP = {"sessions"}                                # directories of the agent's logs never copied
AGENT_FILE_LIMIT = 50 * 1024 * 1024                      # a file larger than this is not copied
AGENT_SUFFIXES = (".txt", ".json", ".jsonl", ".md", ".log")
COPY_DIR = "hermit-copy"                                 # the workspace's folders harborside copied back (copy_back)
COLLECT_DIR = "hermit-collect"                           # the CLI's home files it brought out (collect)
MEMORY = "memory"
TOOL_AUDIT = "tool-calls.jsonl"
REDACTED = "[redacted]"


def seconds(timing: dict | None) -> float | None:
    """A Harbor TimingInfo's duration in seconds."""
    if not timing or not timing.get("started_at") or not timing.get("finished_at"):
        return None
    stamps = [datetime.fromisoformat(str(timing[k]).replace("Z", "+00:00")) for k in ("started_at", "finished_at")]
    return round((stamps[1] - stamps[0]).total_seconds(), 1)


def model_calls(trajectory: Path) -> int:
    """The ATIF steps carrying token metrics: one per model call (none of a file that is missing, no JSON object, or
    nested past the JSON parser's depth)."""
    try:
        steps = json.loads(trajectory.read_text(encoding="utf-8")).get("steps") or []
    except (OSError, ValueError, AttributeError, RecursionError):
        return 0
    return sum(1 for s in steps if isinstance(s, dict) and s.get("metrics"))


def open_regular(path: Path) -> int | None:
    """`path` opened for reading as a regular file, never through a link at its name (O_NOFOLLOW), else None: what the
    task's container could write is read only so, since a link it left there could name a file of this machine."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        if stat.S_ISREG(os.fstat(fd).st_mode):
            return fd
    except OSError:
        pass
    os.close(fd)
    return None


def regular_text(path: Path) -> str | None:
    """A regular file's text (open_regular), or None: missing, unreadable, a link, a pipe or a directory."""
    fd = open_regular(path)
    if fd is None:
        return None
    with os.fdopen(fd, "rb") as f:
        try:
            return f.read().decode("utf-8", errors="replace")
        except OSError:
            return None


def redact_verifier(text: str, patterns) -> str:
    """The verifier's output with each match of `patterns` (verifier_redact: what the learner must not read, a
    test's expected answer) replaced by REDACTED."""
    for pattern in patterns or ():
        text = re.sub(str(pattern), REDACTED, text)
    return text


def _files(src: Path, dst: Path, *, text_only: bool = False, skip=()) -> None:
    """The regular files below `src` (`text_only`: AGENT_SUFFIXES' only), each at most AGENT_FILE_LIMIT, copied under
    `dst`, none in a directory named in `skip`. A link is never followed nor copied: the container wrote `src`, and a
    link there could name a file of this machine."""
    for directory, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(directory, d)) and d not in skip]
        for name in files:
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file() or path.stat().st_size > AGENT_FILE_LIMIT:
                continue
            if text_only and path.suffix not in AGENT_SUFFIXES:
                continue
            target = dst / path.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


def curate(outputs: Path | None, job: Path, verdict: dict, *, result: dict | None = None,
           extra_files: dict[str, Path] | None = None, redact=(), audit: Path | None = None) -> Path:
    """The curated log directory under `job`: the result and the agent's and verifier's outputs from `outputs`
    (a trial directory, or a step's directory holding agent/ and verifier/), the verdict's exception,
    `extra_files` ({name: path}: the job's harbor.log, the stream's driver.log), the tools' `audit` log; `redact`
    the verifier_redact patterns."""
    from hermit.redact import redact_tree
    log = job / "hermit-log"
    shutil.rmtree(log, ignore_errors=True)
    (log / "agent").mkdir(parents=True)
    (log / "verifier").mkdir()
    for name, path in {"job.yaml": job / "job.yaml", "harbor.log": job / "harbor.log", **(extra_files or {})}.items():
        if path.is_file():
            shutil.copy2(path, log / name)
    if audit is not None and Path(audit).is_file():
        shutil.copy2(audit, log / TOOL_AUDIT)
    if verdict.get("exception_type") or verdict.get("exception_message"):
        (log / "exception.txt").write_text(f"{verdict.get('exception_type') or verdict['failure_cause']}: "
                                           f"{verdict.get('exception_message', '')}\n", encoding="utf-8")
    if result is not None:
        (log / "result.json").write_text(json.dumps(result, indent=1, default=str) + "\n", encoding="utf-8")
        trace = ((result.get("exception_info") or {}).get("exception_traceback"))
        if trace:
            with open(log / "exception.txt", "a", encoding="utf-8") as f:
                f.write("\n" + str(trace))
    if outputs is not None:
        _outputs(Path(outputs), log, result, redact)
    redact_tree(log)
    return log


def _outputs(outputs: Path, log: Path, result: dict | None, redact) -> None:
    if result is None and (outputs / "result.json").is_file():
        shutil.copy2(outputs / "result.json", log / "result.json")
    agent = outputs / "agent"
    if agent.is_dir():
        for child in sorted(agent.iterdir()):
            if child.is_symlink():
                continue
            if child.is_dir():
                if child.name in AGENT_SKIP:
                    continue
                if child.name in ("hook-state", ".hermit"):
                    _files(child, log / "agent" / child.name)
                elif child.name == COLLECT_DIR:              # Codex's collected session logs are its transcripts
                    _files(child, log / "agent" / COLLECT_DIR, text_only=True, skip=AGENT_SKIP)
                elif child.name == COPY_DIR and (child / MEMORY).is_dir() and not (child / MEMORY).is_symlink():
                    _files(child / MEMORY, log / MEMORY)
                continue
            # a regular file only: a FIFO or a device the session left under a text name is never opened
            if child.suffix in AGENT_SUFFIXES and child.is_file() and child.stat().st_size <= AGENT_FILE_LIMIT:
                shutil.copy2(child, log / "agent" / child.name)
    verifier = outputs / "verifier"
    text = regular_text(verifier / "test-stdout.txt")
    if text is not None:
        text = redact_verifier(text, redact)
        if len(text) > STDOUT_TAIL:
            text = f"[... {len(text) - STDOUT_TAIL} characters dropped ...]\n" + text[-STDOUT_TAIL:]
        (log / "verifier" / "test-stdout.txt").write_text(text, encoding="utf-8")
    for reward in verifier.glob("reward.*") if verifier.is_dir() else []:
        if reward.is_file() and not reward.is_symlink():
            shutil.copy2(reward, log / "verifier" / reward.name)
