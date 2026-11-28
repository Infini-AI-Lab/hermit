#!/usr/bin/env python3
"""OfficeQA from Harbor's dataset, evaluated as the OfficeQA paper evaluates agents: writes tasks/, tasks.jsonl and
dataset.lock beside this file.

    python examples/officeqa_harbor/utils/prepare.py [--relock]

The tasks are harbor-datasets' `datasets/officeqa` at a pinned commit (their questions, answers, corpus image and
metadata), changed in four ways:

- **The instruction** is the paper's agent prompt (arXiv:2603.08655, Appendix E.5) with the corpus at `/app/corpus/`
  and its web-search sentence left out (the agent's network is the model's only), then the question. The agent ends
  its final message with the answer in `<FINAL_ANSWER>` tags.
- **The verifier** (verifier/) scores that final message as the paper does, with the OfficeQA repository's `reward.py`
  at the paper's commit, the copy the tasks carry (checked against its git blob): the first `<FINAL_ANSWER>` at a 0.0%
  allowable relative error, the paper's headline threshold, as the reward; 0.1%, 1% and 5% beside it in reward.json.
  Harbor's verifier reads
  `/app/answer.txt` at 1%.
- **The image**: the corpus image, pinned by digest (the tasks name `:latest`), with Codex installed (Dockerfile), so
  that a trial installs nothing.
- **The network** follows a trial's phases instead of the tasks' `allow_internet = true`: the agent reaches only the
  login's hosts, the verifier nothing.

Besides, the metadata's `source_files` and `source_docs` (the bulletin and the page that hold the answer, which the
paper's full-corpus agents are not given) are left out of task.toml and so of tasks.jsonl's rows, with
`environment/resources/manifest.json`, which lists them too; `solution/` (Harbor's oracle, which writes the answer.txt
this verifier does not read) is removed.

tasks/ holds the answers (each task's tests/config.json), which Harbor uploads only after the agent ran, so it stays
out of the repository. dataset.lock, committed, records the digest of every task directory: a later prepare checks the
tasks it writes against it and stops on a difference, unless --relock (after a deliberate change) writes it anew.

A prepare writes the new tasks, index and lock to .staging/ beside them. One prepare runs at a time. It installs the
new files after the lock check and image build, and restores the previous files if installation fails. A prepare
interrupted during installation is rolled back when the next prepare starts.

The set is not prepared with `python -m hermit.harbor.dataset prepare` and a `dataset.ref`: OfficeQA is not in Harbor's
registry at that commit (its adapter is still a pull request), so no ref names it.
"""
import argparse
import errno
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

REPO = "https://github.com/laude-institute/harbor-datasets"
COMMIT = "37db108843a49bb31a592e37a75e2c40dc3f9749"
DATASET = "datasets/officeqa"
# databricks/officeqa's reward.py at 2a2c3b0 (2026-03-02), the scorer of the paper (arXiv:2603.08655, 2026-03-09) and
# the copy Harbor's tasks carry (Apache-2.0); its later revisions add a direct-answer guard and take the last tag
REWARD_BLOB = "196f40cfe0d19b310944699cf17ad2a94962c0fb"         # `git hash-object reward.py` at that commit
IMAGE = "docker.io/benediktstroebl/officeqa-corpus"
DIGEST = "sha256:731237e3b35277ca3c08b1b5e6063c4c6ae0c88a4eb4d7cef201c4f14a3e333d"
CODEX = "0.164.0"                                        # the configs' agent.codex.version
LOCAL_IMAGE = f"officeqa-corpus-codex:{CODEX}"
HOSTS = ["chatgpt.com", "auth.openai.com"]               # the ChatGPT login: the model and its token refresh
HERE = Path(__file__).resolve().parent
OUTPUTS = ("tasks", "tasks.jsonl", "dataset.lock")

# arXiv:2603.08655, Appendix E.5, line for line; the corpus path is this image's, and the sentence "You also have
# access to web search in the case that you need to look something up." is left out.
PROMPT = """\
You are an agent that is an expert in answering questions related to the U.S treasury & economy. Given the question, \
please use the treasury docs -- these are located in the folder /app/corpus/, and you can use file system search to \
look through them, given the tools provided. Note that these documents are very long, so you should not try to read \
the whole file.
If multiple documents report the same metric, use the most up-to-date revision unless the question specifies an exact \
date or document. Do not rely on the first matching value you encounter.
You must always provide an answer that is the result of any computation or reasoning to determine the answer to the \
question.
Use maximum precision in your calculations unless otherwise specified by the question.

REQUIRED FORMAT for completion:
When you have the final answer, keep any final reasoning you used before getting to the answer and then only return \
the value in the XML tags.

<REASONING>
[final reasoning - including steps & sources used...]
</REASONING>
<FINAL_ANSWER>
[value]
</FINAL_ANSWER>

Never respond with follow-up questions.

FAILURE CONDITION: If you do not produce a <FINAL_ANSWER> tag, your response will be considered incomplete and you \
will fail the task.

Question: {question}
"""


def localize(toml: str) -> str:
    """The task's task.toml with the built image and the phases' network, without the answer's source."""
    hosts = ", ".join(f'"{h}"' for h in HOSTS)
    toml = re.sub(r"^source_(files|docs) = .*\n", "", toml, flags=re.M)
    toml, images = re.subn(r'docker_image = "[^"]*"', f'docker_image = "{LOCAL_IMAGE}"', toml)
    toml, agents = re.subn(r"^\[agent\]$", f'[agent]\nnetwork_mode = "allowlist"\nallowed_hosts = [{hosts}]', toml, flags=re.M)
    toml, verifiers = re.subn(r"^\[verifier\]$", '[verifier]\nnetwork_mode = "no-network"', toml, flags=re.M)
    if (images, agents, verifiers) != (1, 1, 1):
        raise ValueError("a task.toml is not in the form this script knows")
    return toml


def instruction(config: dict) -> str:
    """The paper's agent prompt with the task's question (its tests/config.json, the question Harbor's instruction
    opens with)."""
    question = str(config.get("question") or "").strip()
    if not question:
        raise ValueError(f"task {config.get('uid')} has no question")
    return PROMPT.format(question=question)


def blob(data: bytes) -> str:
    import hashlib
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def rewrite(task: Path) -> None:
    """One task in the paper's evaluation: its instruction, its tests (config.json, with the answer, and reward.py,
    the paper's scorer, kept), its task.toml."""
    tests = task / "tests"
    config = json.loads((tests / "config.json").read_text(encoding="utf-8"))
    if not str(config.get("expected_answer") or "").strip():
        raise SystemExit(f"{task.name} has no expected answer")
    if blob((tests / "reward.py").read_bytes()) != REWARD_BLOB:
        raise SystemExit(f"{tests / 'reward.py'} is not the paper's reward.py (git blob {REWARD_BLOB})")
    (task / "instruction.md").write_text(instruction(config), encoding="utf-8")
    for old in tests.iterdir():
        if old.name not in ("config.json", "reward.py"):
            old.unlink()
    for name in ("test.sh", "judge.py"):
        shutil.copy2(HERE / "verifier" / name, tests / name)
    (tests / "test.sh").chmod(0o755)
    (task / "task.toml").write_text(localize((task / "task.toml").read_text(encoding="utf-8")), encoding="utf-8")
    shutil.rmtree(task / "solution", ignore_errors=True)
    (task / "environment" / "resources" / "manifest.json").unlink(missing_ok=True)


def finish_stage(stage: Path) -> None:
    """Restore an unfinished install before removing staging. A failed restore leaves its backups for the next run."""
    if not stage.exists():
        return
    # Older prepare versions kept only a tasks backup. Preserve it when both generations are present.
    legacy = stage / "old"
    if legacy.exists():
        if (HERE / "tasks").exists():
            raise SystemExit(f"unfinished prepare: move the old tasks at {legacy} aside before retrying")
        legacy.rename(HERE / "tasks")
    previous = stage / "previous"
    for name in OUTPUTS:
        backup, live = previous / name, HERE / name
        if backup.exists() or (previous / f"absent-{name}").exists():
            if live.exists():
                live.replace(stage / name)
            if backup.exists():
                backup.replace(live)
    shutil.rmtree(stage)


def install(stage: Path) -> None:
    """Record how to roll back each replacement before publishing it."""
    previous = stage / "previous"
    previous.mkdir()
    for name in OUTPUTS:
        new, live = stage / name, HERE / name
        if not new.exists():  # a checked-in lock is kept unless it was regenerated
            continue
        if live.is_dir():
            live.rename(previous / name)
        elif live.exists():
            os.link(live, previous / name)  # keep the current file readable until replaced
        else:
            (previous / f"absent-{name}").touch()
        new.replace(live)
    previous.rename(stage / "committed")  # one rename commits all replacements, even if cleanup is interrupted


def prepare(relock: bool) -> None:
    out, lock = HERE / "tasks", HERE / "dataset.lock"
    stage = HERE / ".staging"
    finish_stage(stage)
    stage.mkdir()
    new_out, new_index, new_lock = stage / "tasks", stage / "tasks.jsonl", stage / "dataset.lock"
    dataset = [sys.executable, "-m", "hermit.harbor.dataset"]
    try:
        with tempfile.TemporaryDirectory() as tmp:      # outside the checkout: the clone holds the answers
            for args in (["clone", "-q", "--filter=blob:none", "--no-checkout", REPO, tmp],
                         ["-C", tmp, "sparse-checkout", "set", DATASET], ["-C", tmp, "checkout", "-q", COMMIT]):
                subprocess.run(["git", *args], check=True)
            shutil.copytree(Path(tmp) / DATASET, new_out)
        tasks = sorted(p.parent for p in new_out.glob("*/task.toml"))
        for task in tasks:
            rewrite(task)
        subprocess.run([*dataset, "index", str(new_out), "--out", str(new_index)], check=True)
        if lock.exists() and not relock:
            if subprocess.run([*dataset, "check", str(new_out), "--lock", str(lock)]).returncode:
                raise SystemExit(f"the tasks differ from {lock.name}; the previous setup is kept: rerun with --relock "
                                 "if the change is deliberate")
        else:
            subprocess.run([*dataset, "lock", str(new_out), "--out", str(new_lock), "--source",
                            f"{REPO}@{COMMIT}:{DATASET}"], check=True)
        subprocess.run(["docker", "build", "-q", "-t", LOCAL_IMAGE, "--build-arg", f"BASE={IMAGE}@{DIGEST}",
                        "--build-arg", f"CODEX_VERSION={CODEX}", str(HERE)], check=True)
        install(stage)
    finally:
        finish_stage(stage)
    print(f"wrote {out}: {len(tasks)} tasks at harbor-datasets {COMMIT[:9]}, in the paper's evaluation")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--relock", action="store_true", help="write dataset.lock anew instead of checking against it")
    opts = parser.parse_args()
    with (HERE / ".prepare.lock").open("a+b") as writer:
        try:
            if os.name == "nt":
                import msvcrt
                writer.seek(0)
                msvcrt.locking(writer.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(writer, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            parser.exit(1, f"another prepare is running in {HERE}\n")
        prepare(opts.relock)


if __name__ == "__main__":
    main()
