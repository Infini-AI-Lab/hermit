"""AHE-Beta's own demand and delivery pipeline as an Evolver, against a dedicated adapter repository (../README.md:
"AHE-Beta's own Evolver"). Not validated end to end (no run against a real Gitea and OpenAgents): the validated route is
Hermit's Codex Evolver (../prepare.py's run.yaml).

AHE-Beta's team is the learner of the paper's loop (its Algorithm 1: Triage, PM, Architect, then the Developers, after
each round's sessions), so it runs where a learner runs: in the run's sandbox (`ctx.sandbox`, required and enforced),
each process in a view of it that shows nothing of the run's own layout (the adapter, the material, the history, the
output). Each step starts the OpenAgents worker (`openagents vigil` in its clone, as AHE-Beta starts it) and AHE-Beta's
controller (its own Python, `ahe_beta.integrations.hermit.feedback`), each with only what it needs:

    the worker       its clone (read-write), its home, the pinned OpenAgents checkout and its environment (with the
                     development extra its reviews test with), Claude Code's own files, the step's tools folder (the
                     askpass script, and `claude` on PATH for the developers, as AHE-Beta's bin folder holds it), its
                     configuration as this machine keeps it (read-only); the Gitea token and the developers' model
                     variables (delivery.json `worker.env`), with AHE-Beta's permission mode, Git identity, subagent
                     model lock and the run's and round's identity
    the controller   AHE-Beta's code and role definitions (not the checkout's experiments, personas, secrets file or
                     standalone records) and environment, started outside the developers' tree, the round's native
                     session records (read-only: the users' records, which AHE-Beta's controller projects for its
                     roles), the delivery state and clone; the Gitea token and the demand roles' model variables
                     (`controller.env`)

Neither reads the cohort's assignments.json, the run's other folders, or this machine's home, and each is handed only
the variables named for it; the network is the run's sandbox's (`sandbox.evolver.allowed_hosts`: the Gitea host and the
models' hosts, prepare.py), a Gitea on this machine's loopback forwarded into it. The developers learn of the sessions
what the controller publishes to Gitea, as in AHE-Beta: each user's issue, which ends with AHE-Beta's `_auto-collected`
line for their triage (the persona's id, capability tier and error-correction style).

This process runs nothing the team can write and reads no Git state of theirs: it takes the delivered adapter from Gitea
into a mirror of its own (the commit the controller names, checked on main), a Git in the sandbox moves the team's clone
to it, and the binding is kept where neither process reaches. The Git credential is the token in the environment, read
by an askpass script that holds none. A step that cannot deliver raises: the Trainer records it as failed and keeps the
adapter; the binding is written last.
"""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlparse

from hermit.adapter.manifest import files_of, read_manifest, structure_of
from hermit.agent.evolver.base import Evolver, StepResult
from hermit.agent.model.usage import Usage
from hermit.redact import redact_tree
from hermit.sandbox import program_paths

EXAMPLE = Path(__file__).resolve().parents[1]           # examples/ahe_beta_adapter: its helpers, beside this Evolver
for folder in (EXAMPLE / "utils", EXAMPLE):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))
from binding import fingerprint, ignored, record  # noqa: E402
from gitea_runtime import ASKPASS, git_url, loopback, origin, plain  # noqa: E402
from openagents_runtime import OpenAgentsSpec, ensure_checkout, ensure_environment  # noqa: E402

BASE_ENV = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ")   # what each process keeps of this one's environment
COMMIT = re.compile(r"[0-9a-f]{40}")
LOOPBACK = ("localhost", "127.0.0.1")   # the loopback names the sandbox's forwarder serves (it listens on IPv4)
# The developers' Claude Code as AHE-Beta runs it in its worker's container (experiment/controllers/runtime.py and its
# vigil entrypoint): every tool allowed (by default it edits but neither commits nor pushes, and a ticket then waits for
# a person), inside the jail, as a user other than root, committing under its own name.
PERMISSION_MODE = "bypassPermissions"
CLAUDE_CREDENTIALS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")   # one of them
DEMAND = ("DEMAND_LLM_BASE_URL", "DEMAND_LLM_API_KEY", "DEMAND_LLM_MODEL")   # the demand roles' model: all of them
# The worker's PATH, set inside the sandbox (whose own setting puts this machine's Python first): the developers'
# `python` is the OpenAgents environment's, as in AHE-Beta's worker image.
WORKER = 'PATH="$AHE_WORKER_PATH"; export PATH; unset AHE_WORKER_PATH; exec "$@"'
MAX_BLOB, MAX_DELIVERED = 1_000_000, 32_000_000   # bytes of a delivered file and of all of them: past the template's
DEVELOPERS = {"GIT_AUTHOR_NAME": "openagents", "GIT_AUTHOR_EMAIL": "openagents@ahe-beta.local",
              "GIT_COMMITTER_NAME": "openagents", "GIT_COMMITTER_EMAIL": "openagents@ahe-beta.local"}
# The team's clone moved to the delivered commit ($1), in the sandbox: its hooks and configuration are the team's.
CHECKOUT = ('set -e; git fetch -q origin main; if [ -n "$(git status --porcelain)" ]; then '
            'echo "the delivery clone has local changes" >&2; exit 3; fi; git switch -q --detach "$1"')


class RunUnavailable(RuntimeError):
    """AHE-Beta's delivery could not run (its configuration, its checkout, its controller): the step fails and the
    adapter stays as it was; the native receipts stay for the next attempt."""


class AHEBetaEvolver(Evolver):
    def config(self, ctx):
        path = Path(ctx.params["delivery_config"]).expanduser()
        if not path.is_absolute():
            path = ctx.base / path
        cfg = json.loads(path.read_text())
        frozen = Path(cfg["root"]) / "effective-delivery.json"
        if frozen.exists() and json.loads(frozen.read_text()) != cfg:
            raise RunUnavailable(
                "delivery configuration changed after this run started"
            )
        return cfg

    def setup(self, ctx):
        cfg = self.config(ctx)
        self._ready(ctx, cfg)
        self._verify_openagents(cfg)
        # Resume uses the existing binding; never reset a delivered round to round one.
        Path(cfg["root"]).mkdir(parents=True, exist_ok=True)
        (Path(cfg["root"]) / "effective-delivery.json").write_text(
            json.dumps(cfg, indent=2) + "\n"
        )

    @staticmethod
    def _ready(ctx, cfg) -> None:
        """What every step needs and none can mend, refused at setup: before the run pays for a round of sessions."""
        problems = []
        if ctx.sandbox is None or not ctx.sandbox.confines():
            problems.append("the run's sandbox is not enforced (sandbox.enabled, with bubblewrap)")
        if not shutil.which("claude"):
            problems.append("the developers' Claude Code (`claude`) is not on this machine's PATH")
        if os.geteuid() == 0:
            problems.append("it runs as root, where Claude Code refuses to run its tools unattended")
        if not os.environ.get(cfg["tracker"]["token_env"]):
            problems.append(f"${cfg['tracker']['token_env']} holds no Gitea token")
        if not os.access(cfg["ahe_python"], os.X_OK):
            problems.append(f"AHE-Beta's interpreter {cfg['ahe_python']} does not run (uv sync --locked --extra "
                            "experiments, in its checkout)")
        handed = set((cfg.get("worker") or {}).get("env") or ())
        if not any(os.environ.get(name) for name in CLAUDE_CREDENTIALS if name in handed):
            problems.append(f"the developers have no Claude Code credential (one of {', '.join(CLAUDE_CREDENTIALS)})")
        missing = [name for name in DEMAND if not os.environ.get(name)]
        if missing:
            problems.append(f"the demand roles' model is not set ({', '.join(missing)})")
        python = Path(cfg["ahe_python"])
        if not (python.parent.parent / "pyvenv.cfg").is_file() or \
                Path.home().resolve().is_relative_to(python.parent.parent.resolve()):
            problems.append(f"AHE-Beta's interpreter {python} is not a virtual environment's own (bin/python beside "
                            "its pyvenv.cfg): the controller is shown that environment, never more")
        if not cfg.get("openagents") or not cfg.get("worker"):
            problems.append("delivery.json names no OpenAgents worker and checkout (bootstrap.py writes them)")
        host = urlparse(cfg["tracker"]["base_url"]).hostname
        if loopback(host) and host not in LOOPBACK:
            problems.append(f"a Gitea on {host}: on this machine, give it as localhost or 127.0.0.1, which the "
                            "sandbox forwards")
        if plain(cfg["tracker"]["base_url"]):
            problems.append("a Gitea on another machine is reached over https: its token would cross the network")
        if host in ("gitea.com", "www.gitea.com"):
            problems.append("AHE-Beta's Hermit controller cannot read Gitea.com's attachments back (it sets no "
                            "observation transport for them): use a Gitea of your own")
        if problems:
            raise RunUnavailable("AHE-Beta's delivery cannot run: " + "; ".join(problems))

    @staticmethod
    def _verify_openagents(cfg):
        info = cfg.get("openagents")
        if not info:
            return
        try:
            checkout = ensure_checkout(
                OpenAgentsSpec(str(info["repository"]), str(info["commit"])),
                Path(info["checkout"]),
            )
            ensure_environment(checkout)             # here, with the network: the worker's sandbox only runs it
        except (KeyError, OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
            raise RunUnavailable(f"OpenAgents checkout is not pinned and ready: {exc}") from exc

    def step(self, ctx):
        try:
            return self._step(ctx)
        except RunUnavailable:
            raise
        except Exception as exc:
            raise RunUnavailable(
                f"AHE delivery could not complete: {type(exc).__name__}: {exc}"
            ) from exc

    @staticmethod
    def _askpass(cfg, folder: Path) -> tuple[dict, list[str]]:
        """The Git and Gitea credential, as variables (the token) and an askpass script in `folder`."""
        tracker = cfg["tracker"]
        token = os.environ.get(tracker["token_env"])
        if not token:
            raise RunUnavailable(f"${tracker['token_env']} holds no Gitea token")
        folder.mkdir(parents=True, exist_ok=True)
        askpass = folder / "askpass.sh"
        askpass.write_text(ASKPASS)
        askpass.chmod(0o700)
        env = {tracker["token_env"]: token, "AHE_GIT_TOKEN": token, "AHE_GIT_USER": str(tracker["owner"]),
               **origin(tracker["base_url"], str(tracker["owner"])), "GIT_ASKPASS": str(askpass),
               "GIT_TERMINAL_PROMPT": "0"}
        return env, sorted(env)

    @staticmethod
    def _declared(names) -> tuple[dict, list[str]]:
        """The variables delivery.json names for a process, from this one's environment."""
        env = {name: os.environ[name] for name in names or () if os.environ.get(name)}
        return env, sorted(env)

    @staticmethod
    def _base() -> dict:
        return {name: os.environ[name] for name in BASE_ENV if os.environ.get(name)}

    @staticmethod
    def _forward(cfg) -> list[int]:
        """A Gitea on this machine's loopback, re-created inside the sandbox's own network (its proxy leaves loopback
        addresses alone)."""
        url = urlparse(cfg["tracker"]["base_url"])
        if url.hostname in LOOPBACK:
            return [url.port or (443 if url.scheme == "https" else 80)]
        return []

    @staticmethod
    def _start(ctx, role: str, command: list[str], log: Path, **grants):
        """`command` started in a view of the run's sandbox that shows nothing of the run's layout (a view of its own,
        as Hermit's harness2 Evolver gives its editors), with `grants` (Sandbox.wrap's readable, writable, env, home,
        keep, ...): the process, its log and the view, which _end closes."""
        box = ctx.sandbox.view(framework=False, role=role)
        stream = log.open("a")
        try:
            argv, env = box.wrap(command, **grants)
            return subprocess.Popen(argv, env=env, stdout=stream, stderr=subprocess.STDOUT,
                                    start_new_session=True), stream, box
        except BaseException:
            stream.close()
            box.close()
            raise

    def _worker(self, ctx, cfg, call: Path, tools: Path, credentials: tuple[dict, list[str]], round_no: int):
        """The OpenAgents worker, started in the run's sandbox for this step."""
        worker, info = cfg.get("worker"), cfg.get("openagents") or {}
        if not worker or not info:
            raise RunUnavailable("delivery.json names no OpenAgents worker and checkout (bootstrap.py writes them): "
                                 "the Evolver starts the worker in the run's sandbox, never one started by hand")
        claude = shutil.which("claude")                  # the developers' CLI (backend: claude_code), run by name
        if not claude:
            raise RunUnavailable("the developers' Claude Code (`claude`) is not on this machine's PATH")
        real = Path(claude).resolve()
        with open(real, "rb") as program:                # the native installer's: one file; npm's, a node script:
            script = program.read(2) == b"#!"            # its package and its node (Hermit's program_paths)
        clone, checkout = Path(worker["clone"]).resolve(), Path(info["checkout"]).resolve()
        home = Path(worker.get("home") or clone.with_name(clone.name + "-home"))   # not in the controller's state
        home.mkdir(parents=True, exist_ok=True)
        (tools / "bin").mkdir(parents=True, exist_ok=True)   # on PATH, as AHE-Beta's bin folder holds it: a link on
        link = tools / "bin" / "claude"                       # this machine's PATH is not shown inside, only the file
        link.unlink(missing_ok=True)                          # it leads to
        link.symlink_to(Path(claude).resolve())
        declared, names = self._declared(worker.get("env"))
        model = declared.get("ANTHROPIC_MODEL")          # the session's model, as delivery.json hands it over
        own = {"HOME": str(home), "OPENAGENTS_NO_SELF_UPDATE": "1", "OPENAGENTS_NO_UPDATE": "1",
               "AHE_WORKER_PATH": os.pathsep.join([str(checkout / ".venv/bin"), str(tools / "bin"),
                                                   os.environ.get("PATH") or os.defpath]),
               "OPENAGENTS_PERMISSION_MODE": os.environ.get("OPENAGENTS_PERMISSION_MODE") or PERMISSION_MODE,
               **DEVELOPERS,
               # AHE-Beta's: subagents on the session's model, and the identity OpenAgents' invocation records carry
               "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1", **({"CLAUDE_CODE_SUBAGENT_MODEL": model} if model else {}),
               "AUTO_DESIGNER_RUN_ID": str(cfg["identity"]["run_id"]), "AUTO_DESIGNER_ROUND": str(round_no)}
        config = worker.get("config")                    # its configuration, kept by this machine: read-only inside,
        return self._start(                              # so a round's developers cannot change the next round's
            ctx, "ahe-worker",
            ["/bin/sh", "-c", WORKER, "sh", str(checkout / ".venv/bin/openagents"), "vigil", "--parallel",
             str(int(worker.get("parallel") or 1))],
            call / "worker.log",
            readable=[checkout, tools, *(self._script_paths(real) if script else [real]),
                      *(Path(p) for p in worker.get("readable") or ())],
            writable=[clone, home], env={**self._base(), **declared, **credentials[0], **own}, home=home, chdir=clone,
            binds=[(Path(config), str(clone / ".openagents/openagents.yml"), False)] if config else [],
            keep=sorted({*names, *credentials[1], *own}), forward=self._forward(cfg), interpreters=[checkout / ".venv"])

    @staticmethod
    def _script_paths(real: Path) -> list[Path]:
        """What a script `claude` needs (npm's node script, a wrapper): Hermit's program_paths, but never a whole folder
        of the home's own (~/.local for a wrapper in ~/.local/bin): the script itself instead."""
        home = Path.home().resolve()
        return [*(p for p in program_paths("claude") if p.resolve().parent != home and p.resolve() != home), real]

    def _controller(self, ctx, cfg, call: Path, session_roots: list[str], credentials: tuple[dict, list[str]]):
        """AHE-Beta's controller for this round, in the run's sandbox."""
        root, repo, ahe_root = Path(cfg["root"]), Path(cfg["repo"]).resolve(), Path(cfg["ahe_root"]).resolve()
        home = root / "controller-home"
        home.mkdir(parents=True, exist_ok=True)
        declared, names = self._declared((cfg.get("controller") or {}).get("env"))
        own = {"PYTHONPATH": str(ahe_root / "backend"), "HOME": str(home)}
        python = Path(cfg["ahe_python"])
        return self._start(                              # -P: no module of the folder it starts in (the
            ctx, "ahe-controller",                       # developers' tree once was) shadows AHE-Beta's;
            ["/bin/sh", "-c", 'exec "$@"', "sh",         # exec'd inside by its venv's name, which the sandbox would
             str(python), "-P", "-m", "ahe_beta.integrations.hermit.feedback", "--request",   # otherwise resolve to
             str(call / "request.json"), "--result", str(call / "result.json")],              # its base installation
            call / "controller.log",
            readable=[ahe_root / "backend/ahe_beta", ahe_root / "backend/agent_defs",   # its code and role definitions:
                      *(Path(r) for r in session_roots)],                            # not backend/data's records
            writable=[root, repo, call, home], env={**self._base(), **declared, **credentials[0], **own}, home=home,
            chdir=home, keep=sorted({*names, *credentials[1], *own}), forward=self._forward(cfg),
            interpreters=[python.parent.parent])

    def _checkout(self, ctx, cfg, call: Path, tools: Path, credentials: tuple[dict, list[str]], commit: str) -> None:
        """The team's delivery clone moved to `commit`, the next round's tested adapter, which AHE-Beta's controller
        reads there (its adapter contract)."""
        repo = Path(cfg["repo"]).resolve()
        process, log, box = self._start(
            ctx, "ahe-git", ["/bin/sh", "-c", CHECKOUT, "sh", commit], call / "git.log", readable=[tools],
            writable=[repo], env={**self._base(), **credentials[0]}, chdir=repo, keep=credentials[1],
            forward=self._forward(cfg))
        try:
            code = self._wait(ctx, process, 600)
        finally:
            self._end(process, log, box)
        if code != 0:
            raise RunUnavailable(f"the delivery clone did not move to {commit}; inspect {call / 'git.log'}")

    @staticmethod
    def _wait(ctx, process, timeout: float):
        """`process`'s exit code, waited for at most `timeout` seconds and while the Trainer lets the call go on; None
        otherwise."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                return process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                if ctx.cancel is not None and ctx.cancel.is_set():
                    return None
        return None

    @staticmethod
    def _going(ctx) -> None:
        """Refuses to go on once the Trainer stopped the call (its timeout, the budget): it keeps the adapter, so
        nothing may move the clone or the binding past it."""
        if ctx.cancel is not None and ctx.cancel.is_set():
            raise RunUnavailable("AHE delivery stopped by the Trainer; the clone and the binding stay")

    @staticmethod
    def _delivered(cfg, commit: str, structure, candidate: Path, scratch: Path, going=lambda: None) -> None:
        """The adapter's files at `commit` (what the template's contract covers), written into `candidate` from a mirror
        of Gitea's repository that only this process writes, `commit` checked on its main, their sizes before any is
        read; `going` raises once the call must stop."""
        tracker = cfg["tracker"]
        env = {**os.environ, **AHEBetaEvolver._askpass(cfg, scratch)[0]}

        def run(*args) -> bytes:                         # no credential helper of this machine's sees or keeps the
            return subprocess.run(["git", "-c", "credential.helper=", "-C", str(scratch / "mirror.git"), *args],
                                  check=True, capture_output=True, env=env, timeout=600).stdout   # token
        subprocess.run(["git", "init", "-q", "--bare", str(scratch / "mirror.git")], check=True, capture_output=True,
                       env=env)
        url = git_url(tracker["base_url"], tracker["owner"], tracker["repo"])
        run("-c", "transfer.fsckObjects=true", "fetch", "-q", "--no-tags", url, "+refs/heads/main:refs/heads/main")
        try:
            run("merge-base", "--is-ancestor", commit, "refs/heads/main")
        except subprocess.CalledProcessError as exc:
            raise RunUnavailable(f"the delivered commit {commit} is not on the repository's main") from exc
        wanted = []
        for entry in run("ls-tree", "-r", "-l", "-z", commit).decode().split("\0"):
            if not entry:
                continue
            meta, relative = entry.split("\t", 1)
            mode, kind, blob, size = meta.split()
            parts = PurePosixPath(relative).parts
            if relative.startswith("/") or any(part in ("", ".", "..", ".git") for part in parts):
                raise RunUnavailable(f"the delivered tree names a path outside the adapter: {relative!r}")
            if relative == ".hermit/CARD.md":                # the Trainer's, not the team's
                continue
            if not (structure.is_fixed(relative) or structure.learnable_for(relative) is not None):
                continue                                 # OpenAgents' forge metadata and specifications: not adapter
            if kind != "blob" or mode not in ("100644", "100755"):
                raise RunUnavailable(f"the delivered adapter holds a link or a submodule: {relative}")
            if int(size) > MAX_BLOB:
                raise RunUnavailable(f"the delivered {relative} is {int(size)} bytes, past any of the template's")
            wanted.append((relative, mode, blob, int(size)))
        if sum(size for *_, size in wanted) > MAX_DELIVERED:
            raise RunUnavailable("the delivered adapter is larger than the template allows")
        for relative, mode, blob, _ in wanted:
            going()
            target = candidate / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(run("cat-file", "blob", blob))
            target.chmod(0o755 if mode == "100755" else 0o644)

    @staticmethod
    def _native(row) -> bool:
        """Whether a session left the one native record AHE-Beta's feedback import reads (its session store's index:
        one valid entry), and was not rated out of time by this example."""
        user = (row.get("pipeline_extra") or {}).get("user") or {}
        if user.get("outcome") == "timed_out" or not user.get("private"):
            return False
        try:
            entries = json.loads((Path(user["private"]) / "sessions" / "index.json").read_text())
        except (OSError, ValueError):
            return False
        return isinstance(entries, list) and len([e for e in entries if isinstance(e, dict) and
                                                  e.get("evaluation_status", "valid") == "valid"]) == 1

    @staticmethod
    def _end(process, log, box) -> None:
        try:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
        finally:
            log.close()
            box.close()

    def _step(self, ctx):
        cfg = self.config(ctx)
        if ctx.sandbox is None or not ctx.sandbox.confines():
            raise RunUnavailable("AHE-Beta's controller and its OpenAgents worker run only in the run's sandbox, "
                                 "enforced (sandbox.enabled, bubblewrap): the delivery runs model-written commands")
        material = ctx.workspace.learning_material / "current"
        summary = json.loads((material / "summary.json").read_text())
        rows = [
            json.loads(path.read_text())
            for path in sorted((material / "tasks").glob("*.json"))
        ]
        # A session that ran out of its time before the user's verdict is rated 1 by this example, with no native
        # session of AHE-Beta's (nor one its feedback import accepts): its rating stands, its evidence is not given.
        native = [row for row in rows if row.get("status") == "ok" and self._native(row)]
        if len(rows) != cfg["sessions_per_round"] or any(
            row["status"] != "ok" or not ((row.get("pipeline_extra") or {}).get("user") or {}).get("private")
            for row in rows
        ):
            raise RunUnavailable("AHE requires a complete valid cohort before feedback")
        of_round = json.loads(Path(cfg["rounds"]).read_text())   # each session's round (prepare.py), no assignment
        rounds = {of_round.get(row["id"]) for row in rows}
        if len(rounds) != 1 or None in rounds:
            raise RunUnavailable("one AHE step must contain one complete logical round")
        round_no = int(rounds.pop())
        binding_path = Path(cfg["binding"])
        if not binding_path.exists():
            raise RunUnavailable("the delivery binding is missing (prepare.py writes it before round 1's sessions, "
                                 "which name its commit)")
        binding = json.loads(binding_path.read_text())
        if binding["round"] != round_no:
            # A crash after delivery may leave the new binding but an unfinished Hermit step.
            if binding["round"] != round_no + 1:
                raise RunUnavailable("delivery binding is outside this logical round")
            # Use the frozen receipt for the old tested commit; do not repeat model execution.
            receipt_path = Path(cfg["root"]) / f"round-{round_no}-adapter.json"
            binding["harness_sha"] = json.loads(receipt_path.read_text())[
                "tested_adapter"
            ]
        if not native:
            raise RunUnavailable("no session of the round has a native record AHE-Beta's feedback import accepts")
        session_roots = [str(Path(row["pipeline_extra"]["user"]["private"]).resolve()) for row in native]
        request = {
            key: cfg[key]
            for key in (
                "root",
                "repo",
                "tracker",
                "identity",
                "roster",
                "definitions_root",
                "delivery_timeout",
                "sessions_per_round",
            )
        }
        request.update(round=round_no, user_sha=binding["harness_sha"], session_roots=session_roots,
                       sessions_per_round=len(session_roots))
        call = Path(cfg["root"]) / "calls" / f"round-{round_no}"
        call.mkdir(parents=True, exist_ok=True)
        (call / "request.json").write_text(json.dumps(request, indent=2) + "\n")
        tools = call / "tools"                           # all the worker is shown of the step's folder
        credentials = self._askpass(cfg, tools)
        worker, worker_log, worker_box = self._worker(ctx, cfg, call, tools, credentials, round_no)
        try:
            process, log, box = self._controller(ctx, cfg, call, session_roots, credentials)
            try:
                while True:
                    try:
                        code = process.wait(timeout=1)
                        break
                    except subprocess.TimeoutExpired:
                        if ctx.cancel is not None and ctx.cancel.is_set():
                            raise RunUnavailable(
                                "AHE delivery cancelled; native receipts retained"
                            )
                        if worker.poll() is not None:    # its developers are gone: no ticket would be delivered
                            raise RunUnavailable(f"the OpenAgents worker exited ({worker.poll()}) during the "
                                                 f"delivery; inspect {call / 'worker.log'}")
            finally:
                self._end(process, log, box)
        finally:
            self._end(worker, worker_log, worker_box)
        if code:
            raise RunUnavailable(
                f"AHE feedback unavailable; inspect {call / 'controller.log'}"
            )
        receipt = json.loads((call / "result.json").read_text())
        commit = str(receipt.get("delivered_adapter") or "")
        if not COMMIT.fullmatch(commit):
            raise RunUnavailable(f"AHE-Beta's receipt names no delivered commit: {commit[:80]!r}")
        structure = structure_of(read_manifest(ctx.workspace.adapter))
        with tempfile.TemporaryDirectory(prefix="ahe-delivery-") as tmp:
            candidate = Path(tmp) / "adapter"
            candidate.mkdir()
            self._delivered(cfg, commit, structure, candidate, Path(tmp), going=lambda: self._going(ctx))
            left_out = ignored(candidate)                     # Hermit's tree (git add -A) would leave them out
            if left_out:
                raise RunUnavailable(f"this machine's Git ignores the delivered {', '.join(left_out[:5])}: Hermit's "
                                     "adapter tree would leave them out")
            shutil.copytree(candidate, Path(tmp) / "probe")   # the Trainer scrubs a key's value before its commit:
            if redact_tree(Path(tmp) / "probe"):              # bound, these bytes would not be the next round's
                raise RunUnavailable("the delivered adapter holds a key's value (Hermit redacts it): not taken")
            token = os.environ.get(cfg["tracker"]["token_env"]) or ""   # under a name Hermit's redaction misses too
            if token and any(token.encode() in p.read_bytes() for p in candidate.rglob("*") if p.is_file()):
                raise RunUnavailable("the delivered adapter holds the Gitea token: not taken")
            for relative in files_of(ctx.workspace.adapter):
                if relative != ".hermit/CARD.md":
                    (ctx.workspace.adapter / relative).unlink()
            for folder in sorted((p for p in ctx.workspace.adapter.rglob("*") if p.is_dir() and ".git" not in
                                  p.relative_to(ctx.workspace.adapter).parts), key=lambda p: -len(p.parts)):
                if not any(folder.iterdir()):            # emptied: a file the delivery puts at that path goes there,
                    folder.rmdir()                       # not inside it
            shutil.copytree(candidate, ctx.workspace.adapter, dirs_exist_ok=True)
            if fingerprint(candidate) != fingerprint(ctx.workspace.adapter):
                raise RunUnavailable("the adapter does not hold the delivered files as delivered")
        checked = ctx.tools["check_spec"](path=str(ctx.workspace.adapter))
        if not checked.get("passed"):
            raise RunUnavailable(
                "delivered adapter violates the fixed template; inspect check_spec"
            )
        (ctx.workspace.output / "delivery.json").write_text(
            json.dumps(
                {**receipt, "hermit_tested_commit": summary["adapter_commit"]}, indent=2
            )
            + "\n"
        )
        self._going(ctx)
        self._checkout(ctx, cfg, call, tools, credentials, commit)
        self._going(ctx)
        record(cfg["binding"], adapter=ctx.workspace.adapter, commit=commit, run_id=cfg["identity"]["run_id"],
               round_no=round_no + 1)                    # last: the next round's sessions carry it
        raw = receipt.get("usage", {})
        usage = (
            [
                Usage(
                    model="ahe-demand",
                    input_tokens=raw.get("input_tokens", 0),
                    output_tokens=raw.get("output_tokens", 0),
                    calls=raw.get("event_count", 0),
                    unreported_calls=raw.get("unavailable_count", 0),
                )
            ]
            if raw
            else []
        )
        usage.append(Usage(model="ahe-developers", unreported_calls=1))   # the worker's Claude Code reports none
        return StepResult(
            status="done",
            summary=f"AHE round {round_no}: delivered, from {len(session_roots)} of {len(rows)} sessions' evidence",
            usage=usage,
            extra={"delivery": receipt},
        )
