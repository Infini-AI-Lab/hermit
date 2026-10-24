#!/usr/bin/env python3
"""One `codex exec` session for the Harness² Codex agent (codex_agent.py) in a Docker container of its own that sees only its workspace, a fresh CODEX_HOME and the
corpus, and reaches only the model provider (the OfficeQA example's Codex harness container, docker/Dockerfile).

The container has: the image (Codex 0.162.0 for Linux, python3); the workspace (read-write), the CODEX_HOME
(read-write) and the corpus (read-only), each bound at its own host path, so a link into the corpus resolves inside;
a /tmp of its own; nothing else of the machine. It sits on an internal Docker network with no route out; its only
way out is the egress proxy (docker/egress_proxy.py), which opens tunnels to the model provider's API and nothing
else. Codex's own workspace-write sandbox (bubblewrap) runs inside, so the session's commands write only the
workspace, /tmp and parts of the CODEX_HOME, and have no network at all. bubblewrap needs namespaces, which Docker
allows only with SYS_ADMIN, seccomp and AppArmor off.

    OFFICEQA_CODEX_IMAGE    the image (default officeqa-codex:0.162.0)
    OFFICEQA_CODEX_NETWORK  the internal network (default oq-sandbox)
    OFFICEQA_CODEX_PROXY    the egress proxy (default http://oq-egress:3128)

`python codex_session.py --check <corpus>` reports what is missing (docker, the image, the network, the corpus).
"""
from __future__ import annotations

import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys

IMAGE = os.environ.get("OFFICEQA_CODEX_IMAGE", "officeqa-codex:0.162.0")
NETWORK = os.environ.get("OFFICEQA_CODEX_NETWORK", "oq-sandbox")
PROXY = os.environ.get("OFFICEQA_CODEX_PROXY", "http://oq-egress:3128")
# Docker's default capabilities less three the sessions do not need, plus what bubblewrap needs to make namespaces
SECURITY = ["--cap-drop", "NET_RAW", "--cap-drop", "MKNOD", "--cap-drop", "AUDIT_WRITE", "--cap-add", "SYS_ADMIN",
            "--security-opt", "seccomp=unconfined", "--security-opt", "apparmor=unconfined"]
# fails the session rather than letting it search an empty corpus
CHECK = ('test -d "$1/transformed" && test -d "$1/jsons" || { echo "officeqa: corpus not mounted: $1" >&2; exit 75; }; '
         'shift; exec codex "$@"')


def mount(path: Path, readonly: bool = False) -> list[str]:
    """The folder at the path Codex is given (links resolved only on the machine's side)."""
    return ["--mount", f"type=bind,src={path.resolve()},dst={path}" + (",readonly" if readonly else "")]


def check(corpus: Path) -> list[str]:
    """What a session needs that is missing: docker, the image, the network, the corpus."""
    docker = shutil.which("docker")
    if docker is None:
        return ["docker not found"]
    inspect = lambda *a: subprocess.run([docker, *a], capture_output=True).returncode == 0
    return ([f"no image {IMAGE} (docker/Dockerfile)"] * (not inspect("image", "inspect", IMAGE))
            + [f"no network {NETWORK} (docker network create --internal {NETWORK})"]
            * (not inspect("network", "inspect", NETWORK))
            + [f"{corpus} lacks {d}/" for d in ("transformed", "jsons") if not (Path(corpus) / d).is_dir()]
            + [f"{m} (what the corpus links to) is not a directory" for m in corpus_mounts(Path(corpus)) if not m.is_dir()])


def corpus_mounts(corpus: Path) -> list[Path]:
    """The corpus and whatever its `transformed/` and `jsons/` link to outside it (prepare.py may link them), each to
    bind read-only at its own path so the links resolve inside."""
    corpus = Path(corpus).resolve()
    extra = {p.resolve().parent if p.resolve().is_dir() else p.resolve() for p in (corpus / "transformed", corpus / "jsons")
             if p.is_symlink()}
    return [corpus, *sorted(m for m in extra if not m.is_relative_to(corpus) and not corpus.is_relative_to(m))]


def run(args: list[str], *, work: Path, home: Path, corpus: Path, keys: list[str], timeout: float,
        stdout: Path, corpus_at: tuple[Path, Path] | None = None) -> tuple[int, bool]:
    """`codex <args>` in a fresh container; its stdout and stderr go to `stdout`. Returns (exit code, timed out);
    a session past `timeout` seconds is stopped and its container removed. `corpus_at` = (a directory of the
    corpus, a directory of the workspace) binds the former at the latter too: a real directory inside, which
    `find` and `rg --files` descend into (a link they do not follow)."""
    docker = shutil.which("docker")
    if docker is None:
        raise RuntimeError("officeqa: docker not found")
    work, home, corpus = Path(work).resolve(), Path(home).resolve(), Path(corpus).resolve()
    name = f"oqh2-{work.parent.name}-{secrets.token_hex(3)}"
    # The session runs as this user, not as the container's root: Codex's bubblewrap sandbox makes mount points in
    # the workspace from inside a user namespace, where root cannot write a directory this user owns, and the files
    # the session leaves stay this user's.
    cmd = [docker, "run", "--rm", "--name", name, "--label", "officeqa-codex=1", "--network", NETWORK,
           "--read-only", "--tmpfs", "/tmp:size=4g", "--pids-limit", "2048", "--memory", "6g", *SECURITY,
           "--user", f"{os.getuid()}:{os.getgid()}",
           *mount(work), *mount(home), *(a for m in corpus_mounts(corpus) for a in mount(m, readonly=True)),
           *(["--mount", f"type=bind,src={Path(corpus_at[0]).resolve()},dst={Path(corpus_at[1]).resolve()},readonly"]
             if corpus_at else []),
           "-w", str(work),
           "-e", f"HOME={home}", "-e", f"CODEX_HOME={home}", "-e", f"HTTPS_PROXY={PROXY}",
           *(a for k in keys for a in ("-e", k)),
           IMAGE, "sh", "-c", CHECK, "sh", str(corpus), *args]
    with open(stdout, "w") as out:
        try:
            proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                  timeout=timeout)
            return proc.returncode, False
        except subprocess.TimeoutExpired:
            subprocess.run([docker, "rm", "-f", name], stdin=subprocess.DEVNULL, capture_output=True)
            return -1, True


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--check":
        problems = check(Path(sys.argv[2]))
        sys.exit("; ".join(problems) if problems else 0)
    sys.exit(__doc__)
