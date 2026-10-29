#!/usr/bin/env python3
"""`codex` for SkillOpt's Codex harness, run in a container of its own that sees only its workspace and the corpus and
reaches only the model provider.

SkillOpt runs `codex exec ... --sandbox workspace-write ... -C <rollout>/predictions/<id>/codex_exec ...`
(CODEX_EXEC_PATH names this script). This script runs that same command, unchanged, in a fresh container
(docker/Dockerfile; Codex 0.134.0 for Linux) that has:

- files: the image, the workspace (read-write), a fresh CODEX_HOME next to `predictions/` (read-write) and the corpus
  (OFFICEQA_CODEX_CORPUS, read-only), each at its own path, and a /tmp of its own. Nothing else of the machine: no home
  directory, no other rollout, no answers.
- network: an internal Docker network with no route out; its only way out is the egress proxy (docker/egress_proxy.py),
  which opens tunnels to the model provider's API and nothing else. Codex reaches the provider through it (HTTPS_PROXY).
- Codex's own workspace-write sandbox (bubblewrap) inside, as SkillOpt asks for: the session's commands write only the
  workspace, /tmp and parts of the fresh CODEX_HOME, and have no network at all. bubblewrap needs namespaces, which
  Docker allows only with SYS_ADMIN, seccomp and AppArmor off; the session's commands run in user, mount and network
  namespaces of their own.

The session's shell gets only Codex's core environment variables (no API key, no proxy). A container outlives a
`docker run` that is killed (SkillOpt's 180 s timeout), so a watcher removes it as soon as `docker run` ends.

    OFFICEQA_CODEX_IMAGE    the image (default officeqa-codex:0.134.0)
    OFFICEQA_CODEX_NETWORK  the internal network (default oq-sandbox)
    OFFICEQA_CODEX_PROXY    the egress proxy (default http://oq-egress:3128)
CODEX_HOME names the home whose config.toml (the provider settings) each rollout's home starts from.
    OFFICEQA_CODEX_AUTH     a Codex login (auth.json) copied into each rollout's home: the user's ChatGPT login
                            (agent.params.login: chatgpt); the proxy must then let chatgpt.com:443 and
                            auth.openai.com:443 through
`codex_sandbox.py --check` reports what is missing (agent.py runs it before the first session).
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

IMAGE = os.environ.get("OFFICEQA_CODEX_IMAGE", "officeqa-codex:0.134.0")
NETWORK = os.environ.get("OFFICEQA_CODEX_NETWORK", "oq-sandbox")
PROXY = os.environ.get("OFFICEQA_CODEX_PROXY", "http://oq-egress:3128")
# Docker's default capabilities less three the sessions do not need, plus what bubblewrap needs to make namespaces
SECURITY = ["--cap-drop", "NET_RAW", "--cap-drop", "MKNOD", "--cap-drop", "AUDIT_WRITE", "--cap-add", "SYS_ADMIN",
            "--security-opt", "seccomp=unconfined", "--security-opt", "apparmor=unconfined"]
# fails the run rather than letting a session search an empty corpus
CHECK = ('test -d "$1/transformed" && test -d "$1/jsons" || { echo "officeqa: corpus not mounted: $1" >&2; exit 75; }; '
         'shift; exec codex "$@"')


def settings(template: str) -> str:
    """The template's settings (projects Codex trusted dropped), the session's shell given only the core variables:
    without its shell snapshot, which would bring back the whole environment of the shell Codex took it from."""
    lines, skip = [], False
    for line in template.splitlines():
        if line.startswith("["):
            skip = line.startswith("[projects.")
        if not skip:
            lines.append(line)
    return "\n".join(["features.shell_snapshot = false", *lines, "", "[shell_environment_policy]",
                      'inherit = "core"',                                # HOME, PATH, SHELL, TMPDIR, USER, ...
                      'exclude = ["*KEY*", "*TOKEN*", "*SECRET*"]', ""])


def prepare_home(home: Path, template: str, auth: str | None) -> None:
    """A rollout's Codex home: the settings, and with a login (OFFICEQA_CODEX_AUTH) its copy. A rollout's later
    sessions keep the copy they have, with whatever Codex refreshed in it."""
    home.mkdir(exist_ok=True)
    if not (home / "config.toml").exists():
        (home / "config.toml").write_text(settings(template))
    if auth and not (home / "auth.json").exists():
        shutil.copy2(auth, home / "auth.json")
        os.chmod(home / "auth.json", 0o600)


def mount(path: Path, readonly: bool = False) -> list[str]:
    """The folder at the path Codex is given (links resolved only on the machine's side)."""
    return ["--mount", f"type=bind,src={path.resolve()},dst={path}" + (",readonly" if readonly else "")]


def check() -> list[str]:
    """What a session needs that is missing: docker, the image, the network, the provider's key, the corpus."""
    docker = shutil.which("docker")
    if docker is None:
        return ["docker not found"]
    inspect = lambda *a: subprocess.run([docker, *a], capture_output=True).returncode == 0
    template = (Path(os.environ["CODEX_HOME"]) / "config.toml").read_text()
    corpus = Path(os.environ["OFFICEQA_CODEX_CORPUS"])
    auth = os.environ.get("OFFICEQA_CODEX_AUTH")
    return ([f"no Codex login at {auth}"] * bool(auth and not Path(auth).is_file())
            + [f"no image {IMAGE} (docker/Dockerfile)"] * (not inspect("image", "inspect", IMAGE))
            + [f"no network {NETWORK} (docker network create --internal {NETWORK})"]
            * (not inspect("network", "inspect", NETWORK))
            + [f"{k} is not set" for k in re.findall(r'env_key\s*=\s*"([^"]+)"', template) if not os.environ.get(k)]
            + [f"{corpus} lacks {d}/" for d in ("transformed", "jsons") if not (corpus / d).is_dir()])


def main() -> None:
    args = sys.argv[1:]
    if args == ["--check"]:
        problems = check()
        sys.exit("; ".join(problems) if problems else 0)
    work = Path(os.path.abspath(args[args.index("-C") + 1]))
    rollout = work.parents[2]                                 # <rollout>/predictions/<id>/codex_exec
    home = rollout / "codex_home"
    template = (Path(os.environ["CODEX_HOME"]) / "config.toml").read_text()
    prepare_home(home, template, os.environ.get("OFFICEQA_CODEX_AUTH"))
    corpus = Path(os.path.abspath(os.environ["OFFICEQA_CODEX_CORPUS"]))
    keys = sorted(set(re.findall(r'env_key\s*=\s*"([^"]+)"', template)))    # passed by name: not on the command line
    docker = shutil.which("docker")
    if docker is None:
        sys.exit("officeqa: docker not found")
    name = f"oqx-{rollout.name}-{os.getpid()}"
    # The session runs as this user, not as the container's root: Codex's bubblewrap sandbox makes mount points in
    # the workspace from inside a user namespace, where root cannot write a directory this user owns, and the files
    # the session leaves stay this user's.
    cmd = [docker, "run", "--rm", "--name", name, "--label", "officeqa-codex=1", "--network", NETWORK,
           "--read-only", "--tmpfs", "/tmp:size=4g", "--pids-limit", "2048", "--memory", "6g", *SECURITY,
           "--user", f"{os.getuid()}:{os.getgid()}",
           *mount(work), *mount(home), *mount(corpus, readonly=True), "-w", str(work),
           "-e", f"HOME={home}", "-e", f"CODEX_HOME={home}", "-e", f"HTTPS_PROXY={PROXY}",
           *(a for k in keys for a in ("-e", k)),
           IMAGE, "sh", "-c", CHECK, "sh", str(corpus), *args]

    # The watcher holds the read end of a pipe whose write end only `docker run` keeps; when that ends, however it
    # ends, the read returns and the container goes too. It holds none of the outputs SkillOpt waits on.
    r, w = os.pipe()
    if os.fork() == 0:
        os.setsid()
        os.close(w)
        null = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(null, fd)
        os.read(r, 1)
        subprocess.run([docker, "rm", "-f", name], stdin=null, stdout=null, stderr=null)
        os._exit(0)
    os.close(r)
    os.set_inheritable(w, True)
    os.execv(docker, cmd)


if __name__ == "__main__":
    main()
