"""The template/adapter tree as a git repository. Only the Trainer uses this module.

Root commit = the template; adapter = commit; training run = branch; checkpoints and finals = tags prefixed with the
run name. Each run works in its own git worktree, so runs of one tree do not share a HEAD or an index.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

from harnessadapter.adapter.manifest import MANIFEST, files_of, read_manifest

ROOT_TAG = "root"
INVALID_MARK = "[invalid: fails check_spec; the next commit repairs it]"
_ENV = {"GIT_AUTHOR_NAME": "harnessadapter", "GIT_AUTHOR_EMAIL": "trainer@harnessadapter",
        "GIT_COMMITTER_NAME": "harnessadapter", "GIT_COMMITTER_EMAIL": "trainer@harnessadapter"}


def git(repo: Path, *args: str, check: bool = True, input: str | None = None) -> str:
    env = {**os.environ, **_ENV, "GIT_TERMINAL_PROMPT": "0"}
    run = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env, input=input)
    if check and run.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {run.stderr.strip()}")
    return run.stdout.strip()


class Tree:
    def __init__(self, repo: Path):
        self.repo = Path(repo).resolve()

    # ---------- creation ----------
    @classmethod
    def open_or_create(cls, repo: Path, template: Path) -> "Tree":
        """Open the tree at `repo`, creating it from `template` (the root commit) if it does not exist."""
        repo = Path(repo).resolve()
        tree = cls(repo)
        if (repo / ".git").exists():
            root = tree.root_manifest()
            mine = read_manifest(template)
            if root["template"] != mine["template"]:
                raise ValueError(f"The tree at {repo} belongs to template {root['template']}, not {mine['template']}")
            return tree
        repo.mkdir(parents=True, exist_ok=True)
        git(repo, "init", "-q", "-b", "trunk")
        git(repo, "config", "core.autocrlf", "false")
        for rel in files_of(template):
            target = repo / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(Path(template) / rel, target)
        git(repo, "add", "-A")
        identity = read_manifest(repo)["template"]
        git(repo, "commit", "-q", "-m", f"root: template {identity['id']}@{identity['version']}")
        git(repo, "tag", ROOT_TAG)
        return tree

    def root_manifest(self) -> dict:
        import yaml
        return yaml.safe_load(git(self.repo, "show", f"{ROOT_TAG}:{MANIFEST}"))

    # ---------- refs ----------
    def resolve(self, ref: str) -> str:
        return git(self.repo, "rev-parse", "--verify", f"{ref}^{{commit}}")

    def export(self, ref: str, target: Path) -> Path:
        """Write the files of commit `ref` into `target` (a fresh directory)."""
        target = Path(target)
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        archive = subprocess.run(["git", "-C", str(self.repo), "archive", "--format=tar", self.resolve(ref)],
                                 capture_output=True, check=True).stdout
        subprocess.run(["tar", "-x", "-C", str(target)], input=archive, check=True)
        return target

    def is_valid(self, ref: str) -> bool:
        return INVALID_MARK not in git(self.repo, "log", "-1", "--format=%B", self.resolve(ref))

    def tag(self, name: str, ref: str, *, force: bool = False) -> None:
        git(self.repo, "tag", *(["-f"] if force else []), name, self.resolve(ref))

    # ---------- runs ----------
    def checkout_run(self, branch: str, start: str, path: Path) -> Path:
        """A worktree for one run on `branch`; created from `start` if the branch does not exist yet."""
        path = Path(path).resolve()
        if path.exists() and (path / ".git").exists():
            return path
        exists = git(self.repo, "rev-parse", "--verify", f"refs/heads/{branch}", check=False)
        if exists:
            git(self.repo, "worktree", "add", "-q", str(path), branch)
        else:
            git(self.repo, "worktree", "add", "-q", "-b", branch, str(path), self.resolve(start))
        return path

    @staticmethod
    def head(checkout: Path) -> str:
        return git(checkout, "rev-parse", "HEAD")

    @staticmethod
    def sync(source: Path, checkout: Path) -> None:
        """Make the checkout's files exactly the source adapter's files (git metadata untouched)."""
        source, checkout = Path(source), Path(checkout)
        want = set(files_of(source))
        for rel in files_of(checkout):
            if rel not in want:
                (checkout / rel).unlink()
        for rel in want:
            target = checkout / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / rel, target)
        for directory in sorted((p for p in checkout.rglob("*") if p.is_dir() and ".git" not in p.parts),
                                key=lambda p: len(p.parts), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                pass

    @staticmethod
    def commit(checkout: Path, message: str) -> str | None:
        """Commit the checkout's current files; None when nothing changed."""
        git(checkout, "add", "-A")
        if not git(checkout, "status", "--porcelain"):
            return None
        git(checkout, "commit", "-q", "-F", "-", input=message)
        return git(checkout, "rev-parse", "HEAD")

    @staticmethod
    def reset(checkout: Path, ref: str) -> None:
        git(checkout, "reset", "-q", "--hard", ref)
        git(checkout, "clean", "-q", "-fdx")

    def changed(self, a: str, b: str) -> list[str]:
        out = git(self.repo, "diff", "--name-only", self.resolve(a), self.resolve(b))
        return [line for line in out.splitlines() if line]

    # ---------- the Evolver's read-only history view ----------
    def history_view(self, ref: str, target: Path, limit: int = 50) -> Path:
        """log.jsonl of the ancestry of `ref` (newest first) and one patch per commit, under diffs/."""
        target = Path(target)
        if target.exists():
            shutil.rmtree(target)
        (target / "diffs").mkdir(parents=True)
        fmt = "%H%x1f%P%x1f%cI%x1f%B%x1e"
        raw = git(self.repo, "log", f"-{limit}", f"--format={fmt}", self.resolve(ref))
        with open(target / "log.jsonl", "w") as log:
            for record in filter(None, (r.strip() for r in raw.split("\x1e"))):
                commit, parents, date, body = (record.split("\x1f") + ["", "", "", ""])[:4]
                log.write(json.dumps({"commit": commit, "parents": parents.split(), "date": date,
                                      "message": body.strip()}) + "\n")
                patch = git(self.repo, "show", "--format=", "--patch", commit) if parents else \
                    git(self.repo, "show", "--format=", "--stat", commit)
                (target / "diffs" / f"{commit[:12]}.patch").write_text(patch + "\n")
        (target / "README.md").write_text(
            "Read-only history of the adapter: log.jsonl lists the commits newest first (commit, parents, date, "
            "message); diffs/<commit>.patch holds each commit's changes. The first commit is the template.\n")
        return target
