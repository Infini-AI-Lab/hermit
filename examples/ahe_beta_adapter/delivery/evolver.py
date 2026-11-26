"""AHE-Beta's own demand and delivery pipeline as an Evolver, against a dedicated adapter repository (../README.md:
"AHE-Beta's own Evolver"). Run end to end once, on one round (../README.md: Validation); the route the example
validates further is Hermit's Codex Evolver (../prepare.py's run.yaml).

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
into a mirror of its own (the commit the controller names, checked on main), keeps of it what Hermit's tree will (Git's
add, commit and archive; when that changes a file, the commit holding what it keeps is put on main and bound instead,
so that AHE-Beta's roles read the files the sessions ran), a Git in the sandbox moves the team's clone to it, and the
binding is kept where neither process reaches. The Git credential is the token in the environment, read by an askpass
script that holds none. The developers' Claude Code is the one setup found and ran in the worker's view, checked each
round. A step that cannot deliver fails, with what the team spent (its controller's usage store, from where the
round's first attempt found it): the Trainer keeps the adapter, and the next round's sessions run the last delivered
one, which the binding's history names; the binding is written last. A round AHE-Beta sealed in an attempt the
Trainer never recorded (a crash before its checkpoint) is taken again, nothing of the team rerun. The delivery state
belongs to one Trainer run: a run started over on it is refused, and so is a delivery configuration changed since the
run's setup, or a snapshot of AHE-Beta that is not the commit the run's identity names.
"""

from __future__ import annotations

import contextlib
import hashlib
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
from hermit.sandbox import LOCAL_ANY, program_paths

EXAMPLE = Path(__file__).resolve().parents[1]           # examples/ahe_beta_adapter: its helpers, beside this Evolver
for folder in (EXAMPLE / "utils", EXAMPLE):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))
from binding import bound, entries, fingerprint, record  # noqa: E402
from gitea_runtime import ASKPASS, direct, git_url, loopback, origin, plain  # noqa: E402
from openagents_runtime import OpenAgentsSpec, apart, ensure_checkout, ensure_environment, own_venv  # noqa: E402

BASE_ENV = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ")   # what each process keeps of this one's environment
COMMIT = re.compile(r"[0-9a-f]{40}")
LOOPBACK = ("localhost", "127.0.0.1")   # the loopback names the sandbox's forwarder serves (it listens on IPv4)


def anthropic(url: str) -> bool:
    """Whether `url` is Anthropic's own API over https, its port written or not: the one a subscription token is for."""
    parsed = urlparse(url)
    try:
        port = parsed.port
    except ValueError:                                   # not a port
        return False
    return parsed.scheme == "https" and parsed.hostname == "api.anthropic.com" and port in (None, 443) \
        and "@" not in parsed.netloc


# The developers' Claude Code as AHE-Beta runs it in its worker's container (experiment/controllers/runtime.py and its
# vigil entrypoint): every tool allowed (by default it edits but neither commits nor pushes, and a ticket then waits for
# a person), inside the jail, as a user other than root, committing under its own name.
PERMISSION_MODE = "bypassPermissions"
CLAUDE_CREDENTIALS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")   # one of them
DEMAND = ("DEMAND_LLM_BASE_URL", "DEMAND_LLM_API_KEY", "DEMAND_LLM_MODEL")   # the demand roles' model: all of them
CONTROLLER_IMPORTS = "import ahe_beta.integrations.hermit.feedback; from nexau import Agent, AgentConfig"   # its own
# The worker's PATH, set inside the sandbox (whose own setting puts this machine's Python first): the developers'
# `python` is the OpenAgents environment's, as in AHE-Beta's worker image.
WORKER = 'PATH="$AHE_WORKER_PATH"; export PATH; unset AHE_WORKER_PATH; exec "$@"'
MAX_BLOB, MAX_DELIVERED = 1_000_000, 32_000_000   # bytes of a delivered file and of all of them: past the template's
DEVELOPERS = {"GIT_AUTHOR_NAME": "openagents", "GIT_AUTHOR_EMAIL": "openagents@ahe-beta.local",
              "GIT_COMMITTER_NAME": "openagents", "GIT_COMMITTER_EMAIL": "openagents@ahe-beta.local"}
# The team's clone moved to the delivered commit ($1), in the sandbox: its hooks and configuration are the team's.
CHECKOUT = ('set -e; git fetch -q origin main; if [ -n "$(git status --porcelain)" ]; then '
            'echo "the delivery clone has local changes" >&2; exit 3; fi; git switch -q --detach "$1"')


def _sha256(path: Path) -> str:
    with open(path, "rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def _publish(path: Path, text: str) -> None:
    """`path` written whole (a sibling, then renamed): a stop or a full disk leaves it as it was, never half."""
    partial = path.with_name(f".{path.name}.partial")
    partial.write_text(text)
    os.replace(partial, path)


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
        try:                                             # the run's own copy of its digest: a `root` changed since
            claimed = json.loads((Path(ctx.workspace.state) / "ahe-delivery.json").read_text()).get("config_sha256")
        except (OSError, ValueError, AttributeError):    # would find no frozen copy to compare
            claimed = None
        if frozen.exists() and json.loads(frozen.read_text()) != cfg or claimed not in (None, self._digest(cfg)):
            raise RunUnavailable(
                "delivery configuration changed after this run started"
            )
        return cfg

    @staticmethod
    def _digest(cfg) -> str:
        return hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()

    def setup(self, ctx):
        cfg = self.config(ctx)
        self._ready(ctx, cfg)
        self._verify_openagents(cfg)
        # The delivery state is one Trainer run's: this one's claim is in its state folder, inside its run folder (a
        # restart keeps it, the Trainer restoring that folder); a run started over (resume_mode=disable) has none, and
        # would take an earlier run's sealed rounds for its own (or be refused by AHE-Beta's frozen ones).
        claim = Path(ctx.workspace.state) / "ahe-delivery.json"
        used = None if claim.exists() else self._used(cfg)
        if used:
            raise RunUnavailable(f"the delivery state is an earlier Trainer run's ({used}): resume that run, or "
                                 "bootstrap a new cohort for a new one")
        self._developers(ctx, cfg, setup=True)
        # Resume uses the existing binding; never reset a delivered round to round one.
        Path(cfg["root"]).mkdir(parents=True, exist_ok=True)
        self._controller_starts(ctx, cfg)
        _publish(Path(cfg["root"]) / "effective-delivery.json", json.dumps(cfg, indent=2) + "\n")
        _publish(claim, json.dumps({"run_id": cfg["identity"]["run_id"], "config_sha256": self._digest(cfg)}) + "\n")

    @staticmethod
    def _used(cfg) -> str | None:
        """The rounds the delivery state shows a Trainer run took (a receipt, a call folder, AHE-Beta's recovery
        record, a binding written by a round's delivery), described; None when it shows none."""
        root, rounds = Path(cfg["root"]), set()
        for path in [*root.glob("round-*-adapter.json"), *(root / "calls").glob("round-*")]:
            with contextlib.suppress(ValueError):
                rounds.add(int(path.name.split("-")[1]))
        with contextlib.suppress(OSError, ValueError, TypeError, AttributeError):
            rounds |= {int(r) for r in json.loads((root / "recovery.json").read_text()).get("rounds") or {}}
        with contextlib.suppress(OSError, ValueError, TypeError):
            rounds |= {int(e.get("round") or 0) - 1 for e in entries(json.loads(Path(cfg["binding"]).read_text()))}
        taken = sorted(r for r in rounds if r >= 1)      # a binding of round r: round r - 1's delivery (the seed's: 0)
        return f"rounds {taken}" if taken else None

    @staticmethod
    def _source(cfg) -> str | None:
        """What is wrong with the AHE-Beta the controller runs (and the sessions' simulator): not the snapshot of the
        commit the run's identity names (`controller_sha`, prepare.py), whatever its checkout became since. None when
        it is."""
        snapshot, expected = Path(cfg.get("snapshot") or ""), (cfg.get("identity") or {}).get("controller_sha")
        if not expected or snapshot.name != expected or not (snapshot / "backend/ahe_beta").is_dir():
            return (f"AHE-Beta's snapshot of the run's commit ({expected}) is missing ({snapshot}): prepare.py writes "
                    "it, and the controller and the sessions run it")
        return None

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
        problems += AHEBetaEvolver._credentials(cfg, getattr(ctx.sandbox, "allow", None))
        if not os.access(cfg["ahe_python"], os.X_OK):
            problems.append(f"AHE-Beta's interpreter {cfg['ahe_python']} does not run (uv sync --locked --extra "
                            "experiments, in its checkout)")
        config = (cfg.get("worker") or {}).get("config")
        if not config or not Path(config).is_file():     # this machine's copy, shown read-only: without it the worker
            problems.append("delivery.json names no worker configuration this machine keeps (bootstrap.py writes "
                            "openagents-worker.yml): the worker would read its clone's own")   # reads the clone's
        problems += AHEBetaEvolver._through_worker(cfg) + AHEBetaEvolver._clones(cfg)
        python = Path(cfg["ahe_python"])
        if not (python.parent.parent / "pyvenv.cfg").is_file() or \
                Path.home().resolve().is_relative_to(python.parent.parent.resolve()):
            problems.append(f"AHE-Beta's interpreter {python} is not a virtual environment's own (bin/python beside "
                            "its pyvenv.cfg): the controller is shown that environment, never more")
        if not cfg.get("openagents") or not cfg.get("worker"):
            problems.append("delivery.json names no OpenAgents worker and checkout (bootstrap.py writes them)")
        else:                                            # each process's folders its own, none the cohort's personas
            problems += apart(AHEBetaEvolver._folders(cfg), Path(cfg["rounds"]).parent,
                              shown=[Path(p) for p in cfg["worker"].get("readable") or ()])
        source = AHEBetaEvolver._source(cfg)
        if source:
            problems.append(source)
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
    def _clones(cfg) -> list[str]:
        """The clones bootstrap.py prepared, gone (scratch storage cleared, say): each a folder with its .git (looked
        at, no Git run in a folder a team writes). The sandbox would make an empty folder, and the round fail after its
        sessions were paid for."""
        named = {"the delivery clone": cfg.get("repo"), "the worker's clone": (cfg.get("worker") or {}).get("clone")}
        return [f"{what} {path} is gone (bootstrap.py prepared it)" for what, path in named.items()
                if path and not (Path(path) / ".git").exists()]

    @staticmethod
    def _through_worker(cfg) -> list[str]:
        """The grants named through a folder the worker writes (its clone, its home, as configured or resolved): the
        kept configuration and each `worker.readable`, by their own names, the links their last component leads through
        and where they end. A link there the worker can point elsewhere between rounds: the next round's mount would be
        its choice."""
        worker = cfg.get("worker") or {}
        if not worker.get("clone"):
            return []
        clone = Path(worker["clone"])
        roots = {Path(os.path.abspath(clone)), clone.resolve(), AHEBetaEvolver._home(worker),
                 *([Path(os.path.abspath(worker["home"]))] if worker.get("home") else [])}
        problems = []
        for given in [*(worker.get("readable") or ()), *([worker["config"]] if worker.get("config") else [])]:
            hop, hops = Path(os.path.abspath(given)), []
            while len(hops) < 40:
                hops.append(hop)
                if not hop.is_symlink():
                    break
                hop = Path(os.path.normpath(hop.parent / os.readlink(hop)))
            for hop in [*hops, Path(given).resolve()]:
                if any(hop == root or root in hop.parents for root in roots):
                    problems.append(f"{given} is named through a folder the worker writes ({hop}): keep it apart")
                    break
        return problems

    @staticmethod
    def _credentials(cfg, allowed=None) -> list[str]:
        """What the team's processes are handed and lack: the Gitea token, a Claude Code credential for the developers,
        the demand roles' model, each set on this machine and named for its process (checked at setup and before each
        round's team starts: a run resumed in a shell without them skips setup)."""
        problems = []
        if not os.environ.get(cfg["tracker"]["token_env"]):
            problems.append(f"${cfg['tracker']['token_env']} holds no Gitea token")
        handed = set((cfg.get("worker") or {}).get("env") or ())
        controller = set((cfg.get("controller") or {}).get("env") or ())   # what the controller is given, by name
        given = [name for name in CLAUDE_CREDENTIALS if name in handed and os.environ.get(name)]
        if not given:
            problems.append(f"the developers have no Claude Code credential (one of {', '.join(CLAUDE_CREDENTIALS)})")
        elif len(given) > 1:                             # each would reach every developer, and an Anthropic key the
            problems.append(f"more than one Claude Code credential is set ({', '.join(given)}): each would reach the "
                            "developers (an Anthropic key sent to a gateway's endpoint, say); leave theirs alone set, "
                            "or name only theirs in delivery.json's worker.env")   # gateway's endpoint
        developers = (os.environ.get("ANTHROPIC_BASE_URL") if "ANTHROPIC_BASE_URL" in handed else None) \
            or "https://api.anthropic.com"               # Claude Code's own default
        if given == ["CLAUDE_CODE_OAUTH_TOKEN"] and not anthropic(developers):
            problems.append(f"a Claude subscription token with ANTHROPIC_BASE_URL {developers}: the subscription "
                            "would go there; give that endpoint's own token (ANTHROPIC_AUTH_TOKEN) instead")
        endpoints = [developers, *([os.environ["DEMAND_LLM_BASE_URL"]] if os.environ.get("DEMAND_LLM_BASE_URL")
                                   and "DEMAND_LLM_BASE_URL" in controller else [])]
        local = sorted(u for u in endpoints if loopback(urlparse(u).hostname) and not LOCAL_ANY.match(u))
        if local:                                        # as for Gitea: the sandbox forwards these spellings' ports
            problems.append(f"a model endpoint on this machine at {', '.join(local)}: give it as localhost or "
                            "127.0.0.1, whose port the sandbox forwards")
        plain = sorted(u for u in endpoints if urlparse(u).scheme != "https" and not loopback(urlparse(u).hostname))
        if plain:                                        # a key in the clear, as for the Gitea of another machine
            problems.append(f"a model endpoint on another machine over plain http ({', '.join(plain)}): its key would "
                            "cross the network in the clear")
        if allowed is not None:                          # the names as the run's proxy keeps them (sandbox.py's
            through = {urlparse(a).hostname or a for a in allowed}   # allowlist proxy), each request's host then in
            opened = sorted(h for h in through if loopback(h))
            if opened:                                   # a name lets every port through
                problems.append(f"the run's sandbox lets this machine through ({', '.join(opened)}): its proxy "
                                "would open every port here to the team; a local Gitea or model is forwarded "
                                "(sandbox.evolver.allowed_hosts names none)")
            blocked = sorted({h for h in (urlparse(u).hostname for u in [*endpoints, cfg["tracker"]["base_url"]])
                              if h and not loopback(h) and h not in through})   # them exactly; none: no network
            if blocked:
                problems.append(f"the run's sandbox does not let {', '.join(blocked)} through (sandbox.evolver."
                                "allowed_hosts, from the Gitea and the model variables set when bootstrap.py ran)")
        missing = [name for name in DEMAND if not os.environ.get(name) or name not in controller]
        if missing:
            problems.append(f"the demand roles' model is not set or not handed to the controller (delivery.json "
                            f"controller.env): {', '.join(missing)}")
        return problems

    @staticmethod
    def _folders(cfg, stores=()) -> dict:
        """The folders the delivery grants, by what they are."""
        worker = cfg["worker"]
        return {"the worker's clone": Path(worker["clone"]), "the worker's home": AHEBetaEvolver._home(worker),
                "the OpenAgents checkout": Path(cfg["openagents"]["checkout"]), "the delivery clone": Path(cfg["repo"]),
                "the controller's state": Path(cfg["root"]),
                "the controller's environment": Path(cfg["ahe_python"]).parent.parent,   # it runs these: none the
                **({"AHE-Beta's snapshot": Path(cfg["snapshot"])} if cfg.get("snapshot") else {}),   # worker writes
                # the cohort's own records: the sessions' (by default, or a step's actual stores) and the runs'
                **({f"the sessions' records ({store})": store for store in stores} if stores else
                   {"the sessions' records": Path(cfg["rounds"]).parent / ".logs"}),
                "the Trainer's runs": Path(cfg["rounds"]).parent / "runs"}

    @staticmethod
    def _home(worker: dict) -> Path:
        """The worker's home: delivery.json's, else beside its clone (not in the controller's state)."""
        clone = Path(worker["clone"]).resolve()
        return Path(worker.get("home") or clone.with_name(clone.name + "-home")).resolve()

    def _developers(self, ctx, cfg, setup: bool = False, stores=()) -> tuple[Path, bool]:
        """The developers' Claude Code: the file `claude` led to on this machine's PATH when the run was set up, kept
        with its digest and version beside the binding (where neither process reaches), so that every round runs that
        one (upstream pins one version; an installer's update between rounds would change the developers mid-run).
        Each time, at setup and each step, it is started in a view showing what the worker's shows of it (`claude
        --version`): an installation that view cannot run (a wrapper whose program is not shown) is refused before a
        round of sessions is paid for, and a version that changed (a wrapper's program updated behind it) at a step.
        Only setup records one afresh. The file, and whether it is a script."""
        kept_at = Path(cfg["binding"]).with_name("delivery-developers.json")
        try:
            kept = json.loads(kept_at.read_text())
        except (OSError, ValueError):
            kept = {}
        claimed = (Path(ctx.workspace.state) / "ahe-delivery.json").exists()   # setup ran for this Trainer run
        fresh = kept.get("run_id") != cfg["identity"]["run_id"] or (setup and not claimed)   # none yet, another run's
        if fresh and not setup and claimed:
            raise RunUnavailable("the record of the developers' Claude Code setup kept is gone "
                                 f"({kept_at}): without it no round can show it is the one the run started with")
        if fresh:
            claude = shutil.which("claude")              # the developers' CLI (backend: claude_code), run by name
            if not claude:
                raise RunUnavailable("the developers' Claude Code (`claude`) is not on this machine's PATH")
            real = Path(claude).resolve()
            kept = {"run_id": cfg["identity"]["run_id"], "path": str(real), "link": str(Path(claude).absolute()),
                    "sha256": _sha256(real)}
            # npm's node CLI (`#!/usr/bin/env node`) runs on the node beside its link (nvm's), as program_paths shows
            # it: that one is pinned, not the one a later step's PATH finds
            with open(real, "rb") as program:
                first = program.read(256).split(b"\n")[0]
            words = first[2:].decode(errors="replace").split() if first.startswith(b"#!") else []
            env = bool(words) and words[0].endswith("/env")
            name = next((w for w in words[1:] if not w.startswith("-")), "") if env else ""
            beside = Path(claude).absolute().parent / name
            if name and "/" not in name and beside.is_file():
                kept["interpreter"], kept["interpreter_name"] = str(beside.resolve()), name
            with contextlib.suppress(OSError, ValueError, KeyError, TypeError):   # upstream's pin, for the record
                pin = json.loads((Path(cfg["snapshot"]) / "backend/compatibility.json").read_text())   # the run's
                pin = pin["experiment_runtime"]["claude_code"]
                kept["upstream_pin"] = {"version": pin["version"],
                                        "this_one": pin["linux_x64_sha256"] == kept["sha256"]}
        real = Path(kept["path"])
        if not real.is_file() or _sha256(real) != kept["sha256"]:
            raise RunUnavailable(f"the developers' Claude Code changed since the run was set up ({real}, "
                                 f"{kept.get('version') or 'its version then'}): every round runs that one "
                                 "(reinstall it, or start another run)")
        with open(real, "rb") as program:                # the native installer's: one file; npm's, a node script:
            script = program.read(2) == b"#!"            # its package and its node (Hermit's program_paths)
        self._link = kept.get("link") or "claude"        # what a script needs is found from it, not today's PATH
        self._interpreter = (kept.get("interpreter_name"), kept.get("interpreter"))
        problems = apart(self._folders(cfg, stores), Path(cfg["rounds"]).parent,   # what the worker is shown of it,
                         shown=[*(self._script_paths(real) if script else [real]),   # and besides
                                *(Path(p) for p in (cfg.get("worker") or {}).get("readable") or ())])
        problems += self._through_worker(cfg)
        if problems:
            raise RunUnavailable("the worker would be shown a folder it must not see: " + "; ".join(problems))
        version = self._probe(ctx, cfg, real, script)
        if kept.get("version") not in (None, version):   # a wrapper's program updated behind it, say
            raise RunUnavailable(f"the developers' Claude Code changed since the run was set up "
                                 f"({kept['version']!r}, now {version!r}): every round runs that one")
        if fresh or "version" not in kept:
            _publish(kept_at, json.dumps({**kept, "version": version}, indent=2) + "\n")
        return real, script

    def _probe(self, ctx, cfg, real: Path, script: bool) -> str:
        """`claude --version` in a view of the run's sandbox showing what the worker's shows of Claude Code: the last
        line it printed; RunUnavailable when it does not run there."""
        worker = cfg.get("worker") or {}
        with tempfile.TemporaryDirectory(prefix="ahe-claude-") as tmp:
            tools, home, log = Path(tmp) / "tools", Path(tmp) / "home", Path(tmp) / "claude.log"
            (tools / "bin").mkdir(parents=True)
            home.mkdir()
            (tools / "bin" / "claude").symlink_to(real)
            self._pin_interpreter(tools / "bin")
            own = {"HOME": str(home), "DISABLE_UPDATES": "1",
                   "AHE_WORKER_PATH": os.pathsep.join([str(tools / "bin"), os.environ.get("PATH") or os.defpath])}
            process, stream, box = self._start(
                ctx, "ahe-claude", ["/bin/sh", "-c", WORKER, "sh", "claude", "--version"], log,
                readable=[tools, *(self._script_paths(real) if script else [real]),
                          *(Path(p) for p in worker.get("readable") or ())],
                writable=[home], env={**self._base(), **own}, home=home, chdir=home, keep=sorted(own))
            try:
                code = self._wait(ctx, process, 120)
            finally:
                self._end(process, stream, box)
            said = log.read_text(errors="replace").strip()
        if code != 0:
            raise RunUnavailable(f"the developers' Claude Code ({real}) does not run in the worker's view of the "
                                 f"sandbox (exit {code}: {said[-300:]!r}): a wrapper or a script needs what it runs "
                                 "shown (delivery.json worker.readable)")
        return said.splitlines()[-1] if said else ""

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
               **origin(tracker["base_url"]), "GIT_ASKPASS": str(askpass),
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

    def _worker(self, ctx, cfg, call: Path, tools: Path, credentials: tuple[dict, list[str]], round_no: int,
                stores=()):
        """The OpenAgents worker, started in the run's sandbox for this step."""
        worker, info = cfg.get("worker"), cfg.get("openagents") or {}
        if not worker or not info:
            raise RunUnavailable("delivery.json names no OpenAgents worker and checkout (bootstrap.py writes them): "
                                 "the Evolver starts the worker in the run's sandbox, never one started by hand")
        real, script = self._developers(ctx, cfg, stores=stores)   # the one setup ran, as it runs in this view
        clone, checkout = Path(worker["clone"]).resolve(), Path(info["checkout"]).resolve()
        head = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"], capture_output=True, text=True,
                              timeout=120).stdout.strip()   # (no process writes it: shown to the worker read-only)
        if head != str(info["commit"]):                  # another cohort's bootstrap may have moved a shared one
            raise RunUnavailable(f"the OpenAgents checkout {checkout} is at {head or 'no commit'}, not the pinned "
                                 f"{info['commit']}")
        status = subprocess.run(["git", "-C", str(checkout), "status", "--porcelain", "--untracked-files=all"],
                                capture_output=True, text=True, timeout=120)   # its source as committed (its own
        if status.returncode != 0 or status.stdout.strip():                    # .venv is ignored)
            raise RunUnavailable(f"the OpenAgents checkout {checkout} has changes, or Git cannot tell: "
                                 f"{(status.stdout.strip() or status.stderr.strip()).splitlines()[:3]}")
        try:                                             # its own environment, not a link out made since setup (the
            own_venv(checkout)                           # worker is shown what an interpreter's path leads to)
        except RuntimeError as exc:
            raise RunUnavailable(str(exc)) from exc
        home = self._home(worker)
        home.mkdir(parents=True, exist_ok=True)
        (tools / "bin").mkdir(parents=True, exist_ok=True)   # on PATH, as AHE-Beta's bin folder holds it: a link on
        link = tools / "bin" / "claude"                       # this machine's PATH is not shown inside, only the file
        link.unlink(missing_ok=True)                          # it leads to
        link.symlink_to(real)
        self._pin_interpreter(tools / "bin")
        declared, names = self._declared(worker.get("env"))
        model = declared.get("ANTHROPIC_MODEL")          # the session's model, as delivery.json hands it over
        own = {"HOME": str(home), "OPENAGENTS_NO_SELF_UPDATE": "1", "OPENAGENTS_NO_UPDATE": "1",
               "AHE_WORKER_PATH": os.pathsep.join([str(tools / "bin"), str(checkout / ".venv/bin"),   # the pin first
                                                   os.environ.get("PATH") or os.defpath]),
               "OPENAGENTS_PERMISSION_MODE": declared.get("OPENAGENTS_PERMISSION_MODE") or PERMISSION_MODE,
               **DEVELOPERS,
               # AHE-Beta's: subagents and the light calls on the session's model (a gateway serves no Haiku; an
               # ANTHROPIC_SMALL_FAST_MODEL handed over is kept), and the identity OpenAgents' invocation records carry
               "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1", **({"CLAUDE_CODE_SUBAGENT_MODEL": model} if model else {}),
               **({"ANTHROPIC_SMALL_FAST_MODEL": model} if model and not declared.get("ANTHROPIC_SMALL_FAST_MODEL")
                  else {}),
               "AUTO_DESIGNER_RUN_ID": str(cfg["identity"]["run_id"]), "AUTO_DESIGNER_ROUND": str(round_no)}
        config = worker.get("config")                    # its configuration, kept by this machine: read-only inside,
        if not config or not Path(config).is_file():     # (a bind whose source is gone is skipped: the clone's copy,
            raise RunUnavailable(f"the worker's configuration {config} is gone: bootstrap.py wrote it")   # its own)
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

    def _pin_interpreter(self, bin_: Path) -> None:
        """The interpreter a script `claude` names (`#!/usr/bin/env node`), linked beside it on the tools PATH: the one
        setup found, not the PATH's at this step (an nvm switch between rounds)."""
        name, path = getattr(self, "_interpreter", (None, None))
        if name and path:
            (bin_ / name).unlink(missing_ok=True)
            (bin_ / name).symlink_to(path)

    def _script_paths(self, real: Path) -> list[Path]:
        """What a script `claude` needs (npm's node script, a wrapper): Hermit's program_paths, but never a whole folder
        of the home's own (~/.local for a wrapper in ~/.local/bin): the script itself instead."""
        home = Path.home().resolve()
        found = program_paths(getattr(self, "_link", None) or "claude")
        return [*(p for p in found if p.resolve().parent != home and p.resolve() != home), real]

    def _controller(self, ctx, cfg, call: Path, session_roots: list[str], credentials: tuple[dict, list[str]]):
        """AHE-Beta's controller for this round, in the run's sandbox (its usage store is its own choice: the
        state's demand-usage.jsonl)."""
        root, repo, source = Path(cfg["root"]), Path(cfg["repo"]).resolve(), Path(cfg["snapshot"]).resolve()
        home = root / "controller-home"
        home.mkdir(parents=True, exist_ok=True)
        declared, names = self._declared((cfg.get("controller") or {}).get("env"))
        own = {"PYTHONPATH": str(source / "backend"), "HOME": str(home)}   # the run's commit of AHE-Beta (prepare.py)
        python = Path(cfg["ahe_python"])
        return self._start(                              # -P: no module of the folder it starts in (the
            ctx, "ahe-controller",                       # developers' tree once was) shadows AHE-Beta's;
            ["/bin/sh", "-c", 'exec "$@"', "sh",         # exec'd inside by its venv's name, which the sandbox would
             str(python), "-P", "-m", "ahe_beta.integrations.hermit.feedback", "--request",   # otherwise resolve to
             str(call / "request.json"), "--result", str(call / "result.json")],              # its base installation
            call / "controller.log",
            readable=[source / "backend/ahe_beta", source / "backend/agent_defs",   # its code and role definitions:
                      *(Path(r) for r in session_roots)],                          # not the checkout's own files
            writable=[root, repo, call, home], env={**self._base(), **declared, **credentials[0], **own}, home=home,
            chdir=home, keep=sorted({*names, *credentials[1], *own}), forward=self._forward(cfg),
            interpreters=[python.parent.parent])

    def _controller_starts(self, ctx, cfg) -> None:
        """AHE-Beta's controller interpreter started in a view as each step's controller is, importing what it runs (its
        feedback entry point, NexAU's Agent): an environment without AHE-Beta's dependencies failed only after a paid
        round of sessions, and every step after it."""
        root, source, python = Path(cfg["root"]), Path(cfg["snapshot"]).resolve(), Path(cfg["ahe_python"])
        home = root / "controller-home"
        home.mkdir(parents=True, exist_ok=True)
        log = root / "controller-check.log"
        own = {"PYTHONPATH": str(source / "backend"), "HOME": str(home)}
        process, stream, box = self._start(
            ctx, "ahe-controller-check",
            ["/bin/sh", "-c", 'exec "$@"', "sh", str(python), "-P", "-c", CONTROLLER_IMPORTS],
            log, readable=[source / "backend/ahe_beta", source / "backend/agent_defs"], writable=[home],
            env={**self._base(), **own}, home=home, chdir=home, keep=sorted(own), interpreters=[python.parent.parent])
        try:
            code = self._wait(ctx, process, 300)
        finally:
            self._end(process, stream, box)
        if code != 0:
            raise RunUnavailable(f"AHE-Beta's controller does not start in its environment ({python}, exit {code}: "
                                 f"{log.read_text(errors='replace').strip()[-300:]!r}): uv sync --locked --extra "
                                 "experiments, in its checkout")

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
    def _delivered(cfg, commit: str, structure, candidate: Path, scratch: Path, going=lambda: None) -> list[str]:
        """The adapter's files at `commit` (what the template's contract covers), written into `candidate` from a mirror
        of Gitea's repository that only this process writes, `commit` checked on its main, their sizes before any is
        read; `going` raises once the call must stop. The commit's other .gitattributes files (its root's, say), which
        an archive of it would apply to the adapter's files."""
        tracker = cfg["tracker"]
        env = direct({**os.environ, **AHEBetaEvolver._askpass(cfg, scratch)[0]}, tracker["base_url"])   # this Git's

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
        wanted, attributes = [], []
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
                if PurePosixPath(relative).name == ".gitattributes":
                    attributes.append(relative)
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
        return attributes

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
        return isinstance(entries, list) and len(entries) == 1 and isinstance(entries[0], dict) and \
            entries[0].get("evaluation_status", "valid") == "valid"   # one entry, as its import requires

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
        source = self._source(cfg)
        if source:
            raise RunUnavailable(source)
        material = ctx.workspace.learning_material / "current"
        summary = json.loads((material / "summary.json").read_text())
        rows = [
            json.loads(path.read_text())
            for path in sorted((material / "tasks").glob("*.json"))
        ]
        of_round = json.loads(Path(cfg["rounds"]).read_text())   # each session's round (prepare.py), no assignment
        rounds = {of_round.get(row["id"]) for row in rows}
        if len(rounds) != 1 or None in rounds:
            raise RunUnavailable("one AHE step must contain one complete logical round")
        round_no = int(rounds.pop())
        try:
            binding = json.loads(Path(cfg["binding"]).read_text())
        except (OSError, ValueError) as exc:
            raise RunUnavailable("the delivery binding cannot be read (prepare.py writes it before round 1's sessions, "
                                 f"which name its commit): {exc}") from exc
        # The commit the round's sessions ran, chosen as they chose it (binding.bound): after a failed step, the last
        # delivered one; in a step the Trainer redoes after a crash, the one its adapter is.
        tested = bound(binding, adapter_sha256=fingerprint(ctx.workspace.adapter), round_no=round_no)
        if tested is None or binding.get("run_id") != cfg["identity"]["run_id"]:
            raise RunUnavailable(f"no delivery commit of this run holds the adapter round {round_no}'s sessions ran")
        call = Path(cfg["root"]) / "calls" / f"round-{round_no}"
        call.mkdir(parents=True, exist_ok=True)
        ledger = Path(cfg["root"]) / "demand-usage.jsonl"   # AHE-Beta's controller names its usage store itself
        mark = call / "usage-offset"                     # where the round's first attempt found it: every attempt the
        if not mark.exists():                            # Trainer did not record is after it (a resumed run's budget
            mark.write_text(str(ledger.stat().st_size if ledger.exists() else 0))   # is the last step's)
        since, run_id = int(mark.read_text() or 0), cfg["identity"]["run_id"]
        receipt = self._sealed(cfg, round_no, tested["harness_sha"])
        if receipt is not None:                          # sealed in an attempt the Trainer never recorded: taken
            given = len(receipt.get("session_ids") or ())   # again (upstream refuses the redone round's sessions)
        else:
            try:
                receipt, given = self._feedback(ctx, cfg, rows, round_no, tested["harness_sha"], call)
            except Exception as exc:                     # noqa: BLE001 - what the team spent before it counts
                spent = self._demand(ledger, since, round_no, run_id)
                if not spent and not getattr(exc, "worker_started", False):   # none of the team ran
                    raise
                return StepResult(status="failed", summary=self._why(exc), usage=self._usage(spent))
        usage = self._usage(self._demand(ledger, since, round_no, run_id))
        try:
            changed, commit = self._take(ctx, cfg, receipt, call, round_no, summary)
        except Exception as exc:                         # noqa: BLE001 - nothing delivered; what was spent counts
            return StepResult(status="failed", summary=self._why(exc), usage=usage, extra={"delivery": receipt})
        return StepResult(
            status="done",
            summary=f"AHE round {round_no}: delivered, from {given} of {len(rows)} sessions' evidence"
                    + (f"; Hermit's tree leaves out or changes {', '.join(changed[:5])}, so {commit[:12]}, the "
                       "commit of what it keeps, is on main and bound" if changed else ""),
            usage=usage,
            extra={"delivery": receipt, **({"changed_by_tree": changed, "bound_adapter": commit} if changed else {})},
        )

    @staticmethod
    def _why(exc: BaseException) -> str:
        return str(exc) if isinstance(exc, RunUnavailable) else \
            f"AHE delivery could not complete: {type(exc).__name__}: {exc}"

    def _feedback(self, ctx, cfg, rows, round_no: int, user_sha: str, call: Path) -> tuple[dict, int]:
        """AHE-Beta's controller and OpenAgents worker on the round's evidence: the receipt of the round they delivered,
        and how many sessions they were given."""
        # A session that ran out of its time before the user's verdict is rated 1 by this example, with no native
        # session of AHE-Beta's (nor one its feedback import accepts): its rating stands, its evidence is not given.
        # Nor is a session whose adapter failed before Codex ran (the judge's 0, no rating of its user's to give).
        failed = {row["id"] for row in rows if (row.get("pipeline_extra") or {}).get("adapter_failed")}
        native = [row for row in rows if row.get("status") == "ok" and row["id"] not in failed and self._native(row)]
        if len(rows) != cfg["sessions_per_round"] or any(
            row["status"] != "ok" or not ((row.get("pipeline_extra") or {}).get("user") or {}).get("private")
            for row in rows
        ):
            raise RunUnavailable("AHE requires a complete valid cohort before feedback")
        lost = [row["id"] for row in rows if row["id"] not in failed and not self._native(row)
                and row["pipeline_extra"]["user"].get("outcome") != "timed_out"]   # rated, its record gone: the
                                                                                   # round's evidence is not whole
        if lost:
            raise RunUnavailable(f"the sessions {lost[:5]} were rated but left no native record AHE-Beta's feedback "
                                 "import accepts")
        if not native:
            raise RunUnavailable("no session of the round has a native record AHE-Beta's feedback import accepts")
        session_roots = [str(Path(row["pipeline_extra"]["user"]["private"]).resolve()) for row in native]
        stores = sorted({Path(row["pipeline_extra"]["user"]["private"]).resolve().parent for row in rows})
        lacking = self._credentials(cfg, getattr(ctx.sandbox, "allow", None)) + self._clones(cfg)
        if lacking:                                      # before either process starts
            raise RunUnavailable("AHE-Beta's team cannot run: " + "; ".join(lacking))
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
        request.update(round=round_no, user_sha=user_sha, session_roots=session_roots,
                       sessions_per_round=len(session_roots))
        (call / "request.json").write_text(json.dumps(request, indent=2) + "\n")
        tools = call / "tools"                           # all the worker is shown of the step's folder
        credentials = self._askpass(cfg, tools)
        worker, worker_log, worker_box = self._worker(ctx, cfg, call, tools, credentials, round_no, stores)
        try:
            return self._feedback_run(ctx, worker, cfg, call, session_roots, credentials)
        except Exception as exc:                         # noqa: BLE001 - its developers may have worked: _step counts
            exc.worker_started = True                    # what they spent (unreported) with the demand roles'
            raise
        finally:
            self._end(worker, worker_log, worker_box)

    def _feedback_run(self, ctx, worker, cfg, call: Path, session_roots: list[str], credentials) -> tuple[dict, int]:
        """The controller, its worker running: the receipt of the round they delivered, and how many sessions they
        were given."""
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
                    if worker.poll() is not None:        # its developers are gone: no ticket would be delivered
                        raise RunUnavailable(f"the OpenAgents worker exited ({worker.poll()}) during the "
                                             f"delivery; inspect {call / 'worker.log'}")
        finally:
            self._end(process, log, box)
        if code:
            raise RunUnavailable(
                f"AHE feedback unavailable; inspect {call / 'controller.log'}"
            )
        return json.loads((call / "result.json").read_text()), len(session_roots)

    @staticmethod
    def _sealed(cfg, round_no: int, user_sha: str) -> dict | None:
        """The receipt of round `round_no` when AHE-Beta sealed it in an attempt the Trainer never recorded: its own
        (round-N-adapter.json), else the sealed commit and plan its recovery record holds (written first: a stop
        between the two leaves no receipt, or part of one). None when the round is not sealed; refused when it was
        sealed for another commit than the one this round's sessions ran."""
        root = Path(cfg["root"])
        try:
            receipt = json.loads((root / f"round-{round_no}-adapter.json").read_text())
        except (OSError, ValueError):
            receipt = None
        if not isinstance(receipt, dict) or not receipt.get("delivered_adapter"):
            try:
                held = json.loads((root / "recovery.json").read_text())["rounds"][str(round_no)]
            except (OSError, ValueError, KeyError, TypeError):
                return None
            if not isinstance(held, dict) or not held.get("sealed_sha"):
                return None
            receipt = {"schema_version": 1, "round": round_no, "tested_adapter": held.get("user_sha"),
                       "delivered_adapter": held["sealed_sha"],
                       "session_ids": list((held.get("plan") or {}).get("sessions") or ())}
        if receipt.get("tested_adapter") != user_sha or receipt.get("round") != round_no:
            raise RunUnavailable(f"AHE-Beta sealed round {round_no} for {receipt.get('tested_adapter')}, not for the "
                                 f"commit this round's sessions ran ({user_sha})")
        return {**receipt, "resealed": True}

    @staticmethod
    def _demand(ledger: Path, since: int, round_no: int, run_id: str) -> list[dict]:
        """The demand roles' calls of round `round_no` in AHE-Beta's usage store, from byte `since` (where the round's
        first attempt found it): an event a call, whatever became of the attempt."""
        try:
            with ledger.open("rb") as stream:
                stream.seek(since if since <= ledger.stat().st_size else 0)
                data = stream.read()
        except OSError:
            return []
        events = []
        for line in data.split(b"\n"):                  # one JSON object a line (a U+2028 inside one is its own)
            with contextlib.suppress(ValueError):
                event = json.loads(line)
                if isinstance(event, dict) and event.get("round_no") == round_no and \
                        event.get("run_id") in (None, run_id):
                    events.append(event)
        return events

    @staticmethod
    def _usage(events: list[dict]) -> list[Usage]:
        """What a round spent: its demand roles' calls, each as it reported (its known tokens the larger of its total
        and its parts, a total its parts do not account for kept with the input so that a token budget counts it; a
        call that reported none or part of it unreported; no price applies, the demand model being no Hermit backend),
        and its developers' (their Claude Code reports none)."""
        def known(event, key):
            value = event.get(key)
            return value if isinstance(value, int) and not isinstance(value, bool) else None
        prompt = completion = cached = unreported = 0
        for event in events:
            given, said = known(event, "input_tokens"), known(event, "output_tokens")
            total, hit = known(event, "total_tokens"), known(event, "cached_input_tokens") or 0
            tokens = max(total or 0, (given or 0) + (said or 0), hit + (said or 0))   # cached ones: a lower bound
            prompt, completion = prompt + tokens - (said or 0), completion + (said or 0)
            cached += min(hit, tokens - (said or 0))
            unreported += event.get("usage_source") == "unavailable" or given is None or said is None or \
                (total or 0) > given + said or hit > given   # parts that do not explain it: counted short
        demand = [Usage(model="ahe-demand", input_tokens=prompt, cached_input_tokens=cached, output_tokens=completion,
                        calls=len(events), unreported_calls=unreported)] if events else []
        return [*demand, Usage(model="ahe-developers", unreported_calls=1)]

    def _take(self, ctx, cfg, receipt: dict, call: Path, round_no: int, summary: dict) -> tuple[list[str], str]:
        """The delivered adapter taken into the workspace as Hermit's tree will keep it, the team's clone moved to the
        commit of those files, and the binding the next round's sessions carry written last: what the tree leaves out
        or changes of the delivery, and the commit bound (the delivered one, or the commit of what the tree keeps)."""
        commit = str(receipt.get("delivered_adapter") or "")
        if not COMMIT.fullmatch(commit):
            raise RunUnavailable(f"AHE-Beta's receipt names no delivered commit: {commit[:80]!r}")
        structure = structure_of(read_manifest(ctx.workspace.adapter))
        with tempfile.TemporaryDirectory(prefix="ahe-delivery-") as tmp:
            candidate = Path(tmp) / "adapter"
            candidate.mkdir()
            attributes = self._delivered(cfg, commit, structure, candidate, Path(tmp), going=lambda: self._going(ctx))
            changed = [*self._kept(candidate, Path(tmp), self._tracked(ctx.workspace.adapter)), *attributes]
                                                         # and the attributes an archive of the commit would apply
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
            self._going(ctx)
            if changed:                                  # AHE-Beta's roles read the bound commit (`git archive`): it
                commit = self._canonical(cfg, Path(tmp), commit, candidate, changed)   # is to hold what ran
        (ctx.workspace.output / "delivery.json").write_text(
            json.dumps(
                {**receipt, "hermit_tested_commit": summary["adapter_commit"], "bound_adapter": commit}, indent=2
            )
            + "\n"
        )
        self._going(ctx)
        self._checkout(ctx, cfg, call, call / "tools", self._askpass(cfg, call / "tools"), commit)
        self._going(ctx)
        record(cfg["binding"], adapter=ctx.workspace.adapter, commit=commit, run_id=cfg["identity"]["run_id"],
               round_no=round_no + 1)                    # last: the next round's sessions carry it
        return changed, commit

    def _canonical(self, cfg, scratch: Path, commit: str, candidate: Path, changed: list[str]) -> str:
        """A child of the delivered commit holding what Hermit's tree keeps of it (`changed` as `candidate` has them),
        made in this process's mirror and pushed on main as a fast-forward (main still the delivered commit), so that
        the commit the next round's sessions carry is the files they run."""
        env = {**direct({**os.environ, **self._askpass(cfg, scratch)[0]}, cfg["tracker"]["base_url"]),
               "GIT_INDEX_FILE": str(scratch / "canonical.index"),
               **{f"GIT_{who}_{what}": value for who in ("AUTHOR", "COMMITTER")
                  for what, value in (("NAME", "hermit"), ("EMAIL", "hermit@localhost"))}}

        def run(*args, data: bytes | None = None) -> str:   # no credential helper of this machine's keeps the token
            return subprocess.run(["git", "-c", "credential.helper=", "-C", str(scratch / "mirror.git"), *args],
                                  input=data, check=True, capture_output=True, env=env,
                                  timeout=600).stdout.decode().strip()
        run("read-tree", commit)
        lines = []
        for relative in changed:
            path = candidate / relative
            if path.is_file():
                blob = run("hash-object", "-w", "--stdin", data=path.read_bytes())
                lines.append(f"{'100755' if path.stat().st_mode & 0o100 else '100644'} {blob}\t{relative}")
            else:
                lines.append(f"0 {'0' * 40}\t{relative}")
        run("update-index", "--index-info", data=("\n".join(lines) + "\n").encode())
        when = run("show", "-s", "--format=%cI", commit)   # the delivered commit's time: a replay makes this commit
        env.update(GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)   # again, not a sibling main refuses
        canonical = run("commit-tree", run("write-tree"), "-p", commit, "-m",
                        "hermit: the adapter as Hermit's tree keeps it\n\n" + "\n".join(changed))
        url = git_url(cfg["tracker"]["base_url"], cfg["tracker"]["owner"], cfg["tracker"]["repo"])
        try:
            run("push", "-q", url, f"{canonical}:refs/heads/main")   # refused unless a fast-forward of main
        except subprocess.CalledProcessError as exc:
            raise RunUnavailable("main moved past the delivered commit: the commit of what Hermit's tree keeps of it "
                                 "could not be put on it") from exc
        return canonical

    @staticmethod
    def _tracked(adapter: Path) -> list[str]:
        """The files the Trainer's tree tracks of the adapter (its export to the workspace is a Git repository of it):
        a .gitignore delivered later leaves them tracked there."""
        listed = subprocess.run(["git", "-C", str(adapter), "ls-files", "-z"], capture_output=True, timeout=120)
        return [name for name in listed.stdout.decode(errors="replace").split("\0") if name] \
            if listed.returncode == 0 else []

    @staticmethod
    def _kept(candidate: Path, scratch: Path, tracked=()) -> list[str]:
        """`candidate` made what Hermit's tree keeps of it, which the next rollouts are given: its .gitattributes files
        out (the tree's commit and export would apply them: a file left out, line endings changed; RRSI's stage() drops
        them too), then Git's add (its own .gitignore files, this machine's excludes and attributes), commit and
        archive, as the Trainer's tree does (hermit/tree.py). The paths it left out or changed."""
        def files(root):                                 # __pycache__ too: the tree leaves it out, so must the
            return {p.relative_to(root).as_posix(): (p.read_bytes(), bool(p.stat().st_mode & 0o100))   # commit bound
                    for p in root.rglob("*") if p.is_file()}
        before = files(candidate)
        for path in candidate.rglob(".gitattributes"):
            path.unlink()
        work = scratch / "kept"
        shutil.copytree(candidate, work, ignore=shutil.ignore_patterns("__pycache__"))   # as files_of() leaves it

        def git(*args):                                 # none of this machine's hooks or signing on a scratch commit
            return subprocess.run(["git", "-C", str(work), "-c", "core.hooksPath=/dev/null",
                                   "-c", "commit.gpgsign=false", *args], check=True, capture_output=True).stdout
        git("init", "-q")
        git("config", "core.autocrlf", "false")         # as the tree's own repository sets it
        already = [name for name in tracked if (work / name).is_file()]
        if already:                                      # tracked there before: an ignore rule leaves them tracked
            git("add", "-f", "--", *already)
        git("add", "-A")
        git("-c", "user.name=hermit", "-c", "user.email=hermit@localhost", "commit", "-q", "--allow-empty",
            "-m", "kept")
        archive = git("archive", "--format=tar", "HEAD")
        shutil.rmtree(candidate)
        candidate.mkdir()
        subprocess.run(["tar", "-x", "-C", str(candidate)], input=archive, check=True, capture_output=True)
        after = files(candidate)
        return sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))
