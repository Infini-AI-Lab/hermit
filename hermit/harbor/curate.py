"""The rollout's log: a curated copy of what the trial (or the step) left, without the agent's raw session
transcripts, the CLI's home or a sign-in.

    hermit-log/
      agent-<backend>.jsonl    the CLI's stream, under the name Hermit's own sessions keep it (hermit.agent.base.
                               trajectory.STREAMS, which the methods' readers look for first at a log's top): a table
                               CLI's (agent/agent-<backend>.jsonl), Harbor's own codex's (agent/codex.txt, as
                               agent-codex.jsonl) or claude-code's (agent/claude-code.txt, as agent-claude.jsonl)
      task.md                  the task's (or the step's) instruction, as Harbor gives it to the agent
      prompt.txt               the prompt the CLI was given: the instruction, then the launch plan's prompt_suffix (the
                               memory note), with its prompt_slash_prefix before one that starts with `/`
      last_message.txt         the CLI's last message, as its Base Agent read it (the session's reply)
      hook-calls.jsonl, hook-state/   what the adapter's hooks wrote (their audit, each session's state)
      result.json              the trial's or step's result as Harbor wrote it
      exception.txt            the exception type, message and traceback, when there was one
      harbor.log, job.yaml     the job's own log and config (a trial; a stream's step has the driver's log)
      agent/                   the agent's other outputs: the answer agent's answer.json, Harbor's own agents' text
                               outputs and trajectory.json (ATIF), hermit-collect/ (the CLI's files brought out: its
                               last message); never sessions/ (a CLI's session logs, collected ones included), nor a
                               binary file of the CLI's home (OpenCode's database)
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
HOOK_AUDIT, HOOK_STATE = "hook-calls.jsonl", "hook-state"   # an adapter's hooks' audit and state (HERMIT_HOOK_LOGS)
# the CLI's stream in a trial's agent/ and its name at the log's top: a table CLI's under its own name, Harbor's own
# agents' under the name Hermit's sessions of that CLI keep (hermit.agent.base.trajectory.CODEX_STREAM, CLAUDE_STREAM)
TOP_STREAMS = {**{f"agent-{b}.jsonl": f"agent-{b}.jsonl"
                  for b in ("codex", "claude", "pi", "mini-swe-agent", "opencode", "dsh")},
               "codex.txt": "agent-codex.jsonl", "claude-code.txt": "agent-claude.jsonl"}


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


DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def _files(src: Path, dst: Path, *, text_only: bool = False, skip=()) -> None:
    """The regular files below `src` (`text_only`: AGENT_SUFFIXES' only), each at most AGENT_FILE_LIMIT, copied under
    `dst`, none in a directory named in `skip`. A link is never followed nor copied: the container wrote `src`, and a
    link there could name a file of this machine. The walk holds each directory open and opens what is in it through
    it, never following a link at any name (O_NOFOLLOW), so a link that a process the agent left running (a stream's
    environment stays up between steps) swaps in for a file or a folder while the walk runs is met as a link, never
    followed; an entry that vanishes or cannot be read meanwhile is left out. A failure to write under `dst` (this
    machine's disk) raises."""
    try:
        top = os.open(src, DIR_FLAGS)
    except OSError:
        return
    _walk(top, dst, text_only, frozenset(skip))


def _walk(dir_fd: int, dst: Path, text_only: bool, skip: frozenset) -> None:
    """_files' walk of the directory open at `dir_fd` (closed after it) into `dst`."""
    try:
        try:
            names = sorted(os.listdir(dir_fd))
        except OSError:
            return
        for name in names:
            try:
                st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(st.st_mode):
                if name in skip:
                    continue
                try:
                    child = os.open(name, DIR_FLAGS, dir_fd=dir_fd)
                except OSError:
                    continue
                _walk(child, dst / name, text_only, skip)
            elif stat.S_ISREG(st.st_mode) and not (text_only and Path(name).suffix not in AGENT_SUFFIXES):
                try:
                    fd = os.open(name, FILE_FLAGS, dir_fd=dir_fd)
                except OSError:
                    continue
                _copy_open(fd, dst / name)
    finally:
        os.close(dir_fd)


def _subdir(parent: Path, name: str) -> int | None:
    """`parent/name` open as a directory, each of the two opened without following a link (DIR_FLAGS), the second
    through the first, so that neither a link at `name` nor one swapped in for `parent` is followed; None otherwise."""
    try:
        top = os.open(parent, DIR_FLAGS)
    except OSError:
        return None
    try:
        return os.open(name, DIR_FLAGS, dir_fd=top)
    except OSError:
        return None
    finally:
        os.close(top)


def _copy_open(fd: int, target: Path) -> None:
    """The file open at `fd` (closed after it) copied to `target` with its times, when it is a regular file of at most
    AGENT_FILE_LIMIT; at most that much of it all the same when it grows meanwhile. A read that fails (the
    container's file) ends the copy with what was read; a write that fails (this machine's disk) raises."""
    with os.fdopen(fd, "rb") as f:
        try:
            st = os.fstat(f.fileno())
        except OSError:
            return
        if not stat.S_ISREG(st.st_mode) or st.st_size > AGENT_FILE_LIMIT:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "wb") as out:
            left = AGENT_FILE_LIMIT
            while left > 0:
                try:
                    chunk = f.read(min(1 << 20, left))
                except OSError:
                    break
                if not chunk:
                    break
                out.write(chunk)
                left -= len(chunk)
    try:
        os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
    except OSError:
        pass


def curate(outputs: Path | None, job: Path, verdict: dict, *, result: dict | None = None,
           extra_files: dict[str, Path] | None = None, redact=(), audit: Path | None = None,
           instruction: str | None = None, plan: Path | None = None, reply: str | None = None) -> Path:
    """The curated log directory under `job`: the result and the agent's and verifier's outputs from `outputs`
    (a trial directory, or a step's directory holding agent/ and verifier/), the verdict's exception,
    `extra_files` ({name: path}: the job's harbor.log, the stream's driver.log), the tools' `audit` log; `redact`
    the verifier_redact patterns. At its top, where Hermit's own logs keep them (the module's docstring), the CLI's
    stream, `instruction` (task.md) and the prompt the launch `plan` made of it (prompt.txt), the session's `reply`
    (last_message.txt) and the hooks' files."""
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
        _outputs(Path(outputs), log, result, redact, _plan_stream(plan))
    if instruction:
        (log / "task.md").write_text(instruction if instruction.endswith("\n") else instruction + "\n",
                                     encoding="utf-8")
        (log / "prompt.txt").write_text(_prompt(instruction, plan), encoding="utf-8")
    if reply is not None:
        (log / "last_message.txt").write_text(str(reply), encoding="utf-8")
    redact_tree(log)
    return log


def _prompt(instruction: str, plan: Path | None) -> str:
    """The prompt harborside gives the CLI (harborside/hermit.py): the instruction and the plan's prompt_suffix, the
    plan's prompt_slash_prefix before one that starts with `/`; the instruction alone without a plan."""
    try:
        data = json.loads(Path(plan).read_text(encoding="utf-8")) if plan is not None else {}
    except (OSError, ValueError):
        data = {}
    prompt = instruction + str(data.get("prompt_suffix") or "")
    if prompt.startswith("/") and data.get("prompt_slash_prefix"):
        prompt = str(data["prompt_slash_prefix"]) + prompt
    return prompt


def _plan_stream(plan: Path | None) -> str | None:
    """The stream the launch `plan` names, "" for one that is no CLI's stream (the answer agent's answer.json) or a
    plan that cannot be read; None without a plan, or one that names none (Harbor's own agents: their stream found by
    TOP_STREAMS)."""
    if plan is None:
        return None
    try:
        data = json.loads(Path(plan).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    name = data.get("stream") if isinstance(data, dict) else None
    if name is None:
        return None
    return name if name in TOP_STREAMS else ""


def _outputs(outputs: Path, log: Path, result: dict | None, redact, named: str | None = None) -> None:
    """The trial's outputs into the log; at its top, the CLI's stream: the one its plan `named` (a trial's agent may
    write another CLI's), else the first TOP_STREAMS finds."""
    if result is None and (outputs / "result.json").is_file():
        shutil.copy2(outputs / "result.json", log / "result.json")
    agent = outputs / "agent"
    if agent.is_dir():
        names = TOP_STREAMS if named is None else [named] if named else []
        stream = next((n for n in names if (agent / n).is_file() and not (agent / n).is_symlink()), None)
        for child in sorted(agent.iterdir()):
            if child.is_symlink():
                continue
            if child.is_dir():
                if child.name in AGENT_SKIP:
                    continue
                if child.name == HOOK_STATE:                 # at the top, beside the stream
                    _files(child, log / HOOK_STATE)
                elif child.name == ".hermit":
                    _files(child, log / "agent" / child.name)
                elif child.name == COLLECT_DIR:              # Codex's collected session logs are its transcripts
                    _files(child, log / "agent" / COLLECT_DIR, text_only=True, skip=AGENT_SKIP)
                elif child.name == COPY_DIR and (memory := _subdir(child, MEMORY)) is not None:
                    _walk(memory, log / MEMORY, False, frozenset())     # through the copy's folder, never a link
                continue
            # a regular file only, opened at its own name (open_regular): a FIFO or a device the session left under a
            # text name is never read, nor a link swapped in for the file since the checks above
            if child.suffix in AGENT_SUFFIXES and (fd := open_regular(child)) is not None:
                top = TOP_STREAMS[child.name] if child.name == stream else HOOK_AUDIT if child.name == HOOK_AUDIT \
                    else None
                _copy_open(fd, log / top if top else log / "agent" / child.name)
    verifier = outputs / "verifier"
    text = regular_text(verifier / "test-stdout.txt")
    if text is not None:
        text = redact_verifier(text, redact)
        if len(text) > STDOUT_TAIL:
            text = f"[... {len(text) - STDOUT_TAIL} characters dropped ...]\n" + text[-STDOUT_TAIL:]
        (log / "verifier" / "test-stdout.txt").write_text(text, encoding="utf-8")
    for reward in verifier.glob("reward.*") if verifier.is_dir() else []:
        if (fd := open_regular(reward)) is not None:          # never a link swapped in since the glob
            _copy_open(fd, log / "verifier" / reward.name)
