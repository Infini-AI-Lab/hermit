#!/usr/bin/env python3
"""All-in-one run of the Lean swarm on one problem: the team service, five sandboxed Codex agents, the reactions
and the judge.

    examples/lean_swarm/launch.sh FormalConjectures/ErdosProblems/263.lean erdos_263 [--agents 5] [--cap 7200]

What it does, in order:
  1. prepares the run directory: the shared library, the service config, one workspace per agent (`hermit
     deploy` of the adapter, plus the problem as Problem.lean with its proof link stripped);
  2. starts a model relay (the framework's wire meter, which also records every agent's tokens) and the team
     service (one unix socket per agent, one control socket);
  3. starts the agents, each `codex exec` in a bubblewrap jail with no network: only its own workspace is writable,
     the Lean checkout is read-only with the problem sources hidden, and two sockets are bridged in (the model relay
     and the agent's own team endpoint);
  4. every 5 s posts the agents' status to the service and carries out the commands the adapter's reactions decided
     (interrupt a running session, resume an ended one, each with a text);
  5. grades every changed Problem.lean in a Lean jail; the first proof ends the run, so does the time cap;
     result.json holds the outcome, times, tokens and cost per agent.

Environment: the key variable of the model's backend (FIREWORKS_API_KEY for the default model; `--model` is a
`<backend>/<model id>` reference, see hermit.backends, and the backend must serve the Responses API, which
Codex speaks); COLLAB_REPO (default ~/code/formal-conjectures) and COLLAB_LAKE (default ~/.elan/bin/lake) for the
Lean checkout and toolchain; LEAN_SWARM_RUNS (default ~/code/lean-swarm-runs) for where runs go. Needs codex, bwrap
and socat on PATH. `--dry-run` prepares everything and starts the service, but no agent.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
FRAMEWORK = HERE.parents[1]
ADAPTER = HERE / "adapter"
HOME = Path.home()
REPO = Path(os.environ.get("COLLAB_REPO", HOME / "code" / "formal-conjectures"))
LAKE = os.environ.get("COLLAB_LAKE", str(HOME / ".elan" / "bin" / "lake"))
RUNS = Path(os.environ.get("LEAN_SWARM_RUNS", HOME / "code" / "lean-swarm-runs"))
RELAY_PORT = 3129      # inside the jail: the model relay
DEFAULT_MODEL = "fireworks/accounts/fireworks/models/deepseek-v4p1-flash"
TEAM_PORT = 3130       # inside the jail: the agent's own team endpoint
POLL = 5

# ---------------------------------------------------------------- the task prompt (the same text the results used)
PROMPT = """You are proving one theorem in Lean 4 with Mathlib, offline.

File: {work}/Problem.lean (a copy of formal-conjectures/FormalConjectures/{rel}).
Target: the theorem `{name}` — replace its `sorry` with a complete proof.

Check your work with the `check` tool (from the team library server): it
compiles Problem.lean against Mathlib (about 10 s) and then prints the axioms
the target depends on. Lean's own tactics `exact?`, `apply?`, `rw?`, `#check`
and grepping the Mathlib sources under {repo}/.lake/packages/mathlib/Mathlib
work offline; there is no internet.

What counts as a proof: the target may depend only on the standard axioms
propext, Classical.choice and Quot.sound. It must not depend, directly or
through other lemmas, on anything that is itself unproved: a theorem in this
file whose proof is `sorry`, or a Mathlib result marked `proof_wanted`. If the
axiom list printed by `check` contains sorryAx, the target is not proved.
If you need such a result, or a key lemma that Mathlib does not have, prove it
yourself as a helper lemma.

The target is true as stated: a complete proof exists. Do not try to disprove
it, and do not conclude that it is false; if your reasoning or numerical checks
suggest it is, re-read the definitions and look for the case you are missing.
If the proof needs theory that Mathlib does not have, build that theory
yourself: state and prove the definitions and lemmas you need as helpers, piece
by piece. Missing library support is a reason to write more Lean, not a reason
to stop.

Rules: do not change anything in the file except the target's proof; you may
add helper definitions and lemmas immediately before the target's docstring or
attribute line.
Do not add axioms.

Team: you are agent {me}. {others} are working on the same theorem at the same
time, each in its own workspace. The team shares a lemma library and a channel,
reached through the tools described in the project instructions (AGENTS.md).
The library directory is {lib}.
Your workspace Problem.lean is what gets graded. As soon as any agent's file
proves the theorem, every agent is stopped.

Keep working until the target is proved. Even if the full proof looks out of
reach, keep going and get as far as you can; the session will be ended for
you. Do not end the session yourself while the target is unproved. Keep the
file in its best state as you go.

Finish: as soon as `check` compiles the file without errors and the target's
axiom list contains only propext, Classical.choice and Quot.sound, stop. No
final review or summary is needed.
"""

# ---------------------------------------------------------------- the judge
OK_AXIOMS = {"propext", "Classical.choice", "Quot.sound"}
DUMP_CMD = """
open Lean Elab Command in
run_cmd do
  let env ← getEnv
  for (n, ci) in env.constants.map₂.toList do
    let kind := match ci with
      | .thmInfo _ => "thm" | .defnInfo _ => "def" | .axiomInfo _ => "axiom" | .opaqueInfo _ => "opaque"
      | .inductInfo _ => "ind" | .ctorInfo _ => "ctor" | .recInfo _ => "rec" | .quotInfo _ => "quot"
    let canon (e : Expr) : Expr := e.replace fun s => if s.isAppOf ``sorryAx then some (mkConst ``sorryAx) else none
    let vh := match ci.value? with | some v => toString (canon v).hash | none => "-"
    logInfo m!"DECL {{n}} {{kind}} {{(canon ci.type).hash}} {{vh}}"
    if n == `{name} || n.toString.endsWith ".{name}" then
      let ax ← collectAxioms n
      logInfo m!"AXIOMS of {{n}}: {{ax}}"
"""
AUX = re.compile(r"^(match_\d+|proof_\d+|eq_\d+|_.*|\d+)$")


def _imports(src: str) -> list[str]:
    return [l.strip() for l in src.splitlines() if l.startswith("import ")]


def _decls(out: str) -> dict[str, tuple[str, str, str]]:
    res = {}
    for n, kind, th, vh in re.findall(r"DECL (\S+) (\S+) (\d+) (\S+)", out):
        parts = n.split(".")
        if parts[0] == "_private":
            parts = parts[3:]
        if any(AUX.match(p) for p in parts):
            continue
        res[n] = (kind, th, vh)
    return res


def _axioms(out: str) -> dict[str, set[str]]:
    return {n: {a.strip() for a in ax.split(",") if a.strip()}
            for n, ax in re.findall(r"AXIOMS of (\S+): \[([^\]]*)\]", out, re.S)}


class Judge:
    """A submitted Problem.lean proves the target when its imports are the original's, it compiles, every original
    declaration keeps its statement (and every non-theorem its value), it adds no axiom, and the target depends only
    on the standard axioms. Semantics are compared, not text: helpers may be added anywhere."""

    def __init__(self, orig: str, name: str):
        self.orig, self.name = orig, name
        self._want = None

    def compile(self, path: Path) -> tuple[int, str]:
        try:
            p = subprocess.run(lean_jail_prefix(path.parent) + [LAKE, "env", "lean", str(path)], cwd=REPO,
                               capture_output=True, text=True, timeout=3600)
        except subprocess.TimeoutExpired:
            return 1, f"{path}:1:0: error: compile timed out after 3600 s (judge)"
        return p.returncode, p.stdout + p.stderr

    def original(self, work: Path):
        if self._want is None:
            g = work / "OrigDecls.lean"
            g.write_text(self.orig.rstrip() + "\n" + DUMP_CMD.format(name=self.name))
            rc, out = self.compile(g)
            d, targets = _decls(out), sorted(_axioms(out))
            if rc or not d or not targets:
                raise RuntimeError(f"the original file did not compile or has no target {self.name}:\n{out[-2000:]}")
            self._want = (d, targets)
        return self._want

    def grade(self, cand: str, work: Path) -> str:
        if _imports(cand) != _imports(self.orig):
            return "rejected: imports changed"
        want, targets = self.original(work)
        g = work / "Grade.lean"
        g.write_text(cand.rstrip() + "\n" + DUMP_CMD.format(name=self.name))
        rc, out = self.compile(g)
        errs = [l for l in out.splitlines() if ": error" in l]
        if rc or errs:
            return "fails to compile: " + (errs[0][:200] if errs else f"rc={rc}")
        got, ax = _decls(out), _axioms(out)
        for n, (kind, th, vh) in want.items():
            if n not in got:
                return f"rejected: declaration {n} removed"
            k2, th2, vh2 = got[n]
            if k2 != kind or th2 != th:
                return f"rejected: {n} no longer has its original statement"
            if kind != "thm" and vh2 != vh:
                return f"rejected: {n} no longer has its original definition"
        new_axioms = [n for n, (k, _, _) in got.items() if k == "axiom" and n not in want]
        if new_axioms:
            return "rejected: new axiom " + ", ".join(new_axioms)
        for t in targets:
            if t not in ax:
                return f"rejected: target {t} not found"
            if "sorryAx" in ax[t]:
                return "not proved (sorry)"
            if ax[t] - OK_AXIOMS:
                return "proved with extra axioms: " + ", ".join(sorted(ax[t] - OK_AXIOMS))
        return "PROVED"


# ---------------------------------------------------------------- jails (bubblewrap)
def _system_binds() -> list[str]:
    cmd = ["bwrap", "--unshare-user", "--unshare-net", "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--unshare-cgroup",
           "--die-with-parent", "--new-session", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--ro-bind", "/usr", "/usr"]
    for link in ("/lib", "/lib64", "/bin", "/sbin"):
        if os.path.islink(link):
            cmd += ["--symlink", os.readlink(link), link]
        elif os.path.isdir(link):
            cmd += ["--ro-bind", link, link]
    for etc in ("/etc/ld.so.cache", "/etc/ld.so.conf", "/etc/ld.so.conf.d", "/etc/alternatives", "/etc/ssl", "/etc/pki",
                "/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf", "/etc/passwd", "/etc/group", "/etc/localtime"):
        if os.path.exists(etc):
            cmd += ["--ro-bind", etc, etc]
    return cmd


def _repo_hides() -> list[str]:
    """What of the Lean checkout no agent may read: the git objects (every problem file as committed, proof links
    included), documentation and the problem sources; a compile needs only the compiled imports."""
    out = ["--tmpfs", str(REPO / ".git")]
    for extra in ("site", "docbuild", "scripts", ".github"):
        if (REPO / extra).is_dir():
            out += ["--tmpfs", str(REPO / extra)]
    for d in sorted((REPO / "FormalConjectures").iterdir()):
        if d.is_dir():
            out += ["--tmpfs", str(d)]
    return out


ELAN = HOME / ".elan"


def lean_jail_prefix(rw: Path) -> list[str]:
    """A compile of agent-written Lean (the service's `check`, the judge): no network, toolchain and checkout read-only,
    only `rw` writable. A Lean file can run IO at elaboration time, so an agent's file is never compiled on the open
    host. Returns the command prefix; the lake command follows it."""
    cmd = _system_binds() + ["--ro-bind", str(ELAN), str(ELAN), "--ro-bind", str(REPO), str(REPO)] + _repo_hides()
    cmd += ["--bind", str(rw), str(rw)]
    return cmd + ["--chdir", str(REPO), "--clearenv", "--setenv", "HOME", "/tmp", "--setenv", "LANG", "C.UTF-8",
                  "--setenv", "ELAN_HOME", str(ELAN), "--setenv", "PATH", f"{ELAN}/bin:/usr/bin:/bin", "--"]


def agent_jail(work: Path, home: Path, ro: list[Path], bridges: list[tuple[Path, int]], env: dict[str, str]) -> list[str]:
    """The agent's jail: no network; its workspace and codex home writable, the codex release, the toolchain and the
    checkout read-only; each (unix socket, port) bridged in by socat so the agent reaches it at 127.0.0.1:port."""
    codex_rel = Path(os.path.realpath(shutil.which("codex"))).parents[1]
    cmd = _system_binds()
    bridge = ""
    for sock, port in bridges:
        cmd += ["--bind", str(sock), str(sock)]
        bridge += (f"socat TCP-LISTEN:{port},bind=127.0.0.1,fork,reuseaddr UNIX-CONNECT:{sock} & "
                   f"for i in $(seq 100); do (exec 3<>/dev/tcp/127.0.0.1/{port}) 2>/dev/null && break; sleep 0.1; done; ")
    bridge += 'exec "$@"'
    for p in (codex_rel, ELAN, REPO, *ro):
        cmd += ["--ro-bind", str(p), str(p)]
    cmd += _repo_hides()
    for p in (work, home):
        cmd += ["--bind", str(p), str(p)]
    return cmd + ["--chdir", str(work), "--clearenv", "--setenv", "USER", os.environ.get("USER", "user"),
                  "--setenv", "LANG", "C.UTF-8", "--setenv", "TERM", "xterm-256color",
                  "--setenv", "HOME", str(home), "--setenv", "CODEX_HOME", str(home), "--setenv", "ELAN_HOME", str(ELAN),
                  "--setenv", "PATH", f"{codex_rel}/bin:{ELAN}/bin:/usr/bin:/bin", "--setenv", "TMPDIR", "/tmp",
                  *[x for k, v in env.items() for x in ("--setenv", k, v)],
                  "--", "bash", "-c", bridge, "jail"]


CODEX_FLAGS = [  # the model's own commands may not write /tmp outside the workspace; nothing that reaches outside the jail
    "-c", "sandbox_workspace_write.exclude_slash_tmp=true", "-c", "sandbox_workspace_write.exclude_tmpdir_env_var=true",
    "-c", "sandbox_workspace_write.network_access=true", "-c", 'web_search="disabled"',
    *[x for f in ("apps", "plugins", "remote_plugin", "browser_use", "browser_use_external", "computer_use",
                  "in_app_browser", "image_generation", "multi_agent", "tool_suggest", "skill_mcp_dependency_install")
      for x in ("--disable", f)],
]


def codex_cmd(work: Path, binding, agent: str, prompt: str) -> list[str]:
    """The agent's session: its model on its backend, reached through the relay (bucket = the agent's name)."""
    relay = f"http://127.0.0.1:{RELAY_PORT}/{agent}{binding.backend.path}"
    return ["codex", "exec", "--skip-git-repo-check", "--ignore-user-config", "--json", "-m", binding.model,
            "-c", "model_reasoning_effort=high", "-c", "model_context_window=1048576",   # the experiment's own setting
            *binding.codex_args(base_url=relay),
            "-s", "workspace-write", "-C", str(work), *CODEX_FLAGS,
            "-c", f'mcp_servers.team.url="http://127.0.0.1:{TEAM_PORT}/mcp/"',
            "-c", "mcp_servers.team.startup_timeout_sec=60", "-c", "mcp_servers.team.tool_timeout_sec=1800",
            prompt]


def codex_resume_cmd(cmd: list[str], text: str) -> list[str]:
    """The first-session command turned into `codex exec resume --last` with a new prompt: resume takes no -s / -C,
    so the sandbox mode goes in as config."""
    i = cmd.index("codex")
    head, rest = cmd[:i], cmd[i + 2:-1]
    out, k = [], 0
    while k < len(rest):
        if rest[k] in ("-s", "-C"):
            k += 2
            continue
        out.append(rest[k])
        k += 1
    return head + ["codex", "exec", "resume", "--last"] + out + ["-c", 'sandbox_mode="workspace-write"', text]


# ---------------------------------------------------------------- helpers
class UDS(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float = 30):
        super().__init__("localhost", timeout=timeout)
        self.path_ = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path_)


def ctl(sock: str, method: str, path: str, body=None):
    c = UDS(sock)
    c.request(method, path, body=json.dumps(body) if body is not None else None, headers={"Content-Type": "application/json"})
    r = c.getresponse()
    data = r.read()
    c.close()
    return json.loads(data or b"{}")


def agent_busy(work: Path, now: float, window: int = 2_000_000, max_age: float = 600) -> bool:
    """True when the agent's event log has a command or tool call started and not completed (a compile still running)."""
    try:
        with open(work / "events.jsonl", "rb") as ef:
            ef.seek(0, 2)
            sz = ef.tell()
            ef.seek(max(0, sz - window))
            lines = ef.read().decode("utf-8", "replace").strip().splitlines()
        started, done = {}, set()
        for l in lines[1:] if sz > window else lines:
            try:
                e = json.loads(l)
            except Exception:
                continue
            it = e.get("item") or {}
            if it.get("type") not in ("command_execution", "mcp_tool_call"):
                continue
            if e.get("type") == "item.started":
                started[it.get("id")] = True
            elif e.get("type") == "item.completed":
                done.add(it.get("id"))
        pending = [i for i in started if i not in done]
        return bool(pending) and now - os.path.getmtime(work / "events.jsonl") < max_age
    except Exception:
        return False


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(pred, seconds: float, what: str):
    for _ in range(int(seconds * 10)):
        if pred():
            return
        time.sleep(0.1)
    raise RuntimeError(f"{what} did not come up in {seconds:.0f} s")


def killpg(proc: subprocess.Popen | None):
    if proc and proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            time.sleep(2)
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def cost(ledger: dict, price: dict | None):
    """USD from the relay's ledger at the backend's price for the model; None without a price."""
    if not price or not ledger:
        return None
    c = ledger.get("cacheRead", 0)
    return round(((ledger.get("input", 0) - c) * price.get("input", 0) + c * price.get("cached_input", price.get("input", 0))
                  + ledger.get("output", 0) * price.get("output", 0)) / 1e6, 4)


# ---------------------------------------------------------------- the run
def main() -> None:
    ap = argparse.ArgumentParser(description="Run the Lean swarm on one problem.")
    ap.add_argument("problem", help="a .lean file, or a path under <COLLAB_REPO>/FormalConjectures")
    ap.add_argument("theorem", help="the target theorem's name (its proof is `sorry` in the file)")
    ap.add_argument("--agents", type=int, default=5)
    ap.add_argument("--cap", type=int, default=7200, help="wall-clock cap in seconds")
    ap.add_argument("--model", default=DEFAULT_MODEL, help="<backend>/<model id> (hermit backends list)")
    ap.add_argument("--out", type=Path, help="the run directory (default: <LEAN_SWARM_RUNS>/<problem>__<theorem>)")
    ap.add_argument("--dry-run", action="store_true", help="prepare the run and start the service, but no agent")
    a = ap.parse_args()

    sys.path.insert(0, str(FRAMEWORK))
    from hermit.backends import problems_of, registry, resolve
    try:
        binding = resolve(a.model, registry())
    except ValueError as exc:
        sys.exit(f"REFUSING: {exc}")
    key = binding.key()
    for tool in ("codex", "bwrap", "socat"):
        if not shutil.which(tool):
            sys.exit(f"REFUSING: {tool} is not on PATH")
    problems = problems_of(binding, "responses")
    if problems and not a.dry_run:
        sys.exit("REFUSING: " + "; ".join(problems))
    if not (REPO / "FormalConjectures").is_dir() or not Path(LAKE).exists():
        sys.exit(f"REFUSING: no Lean checkout at {REPO} or no lake at {LAKE} (COLLAB_REPO, COLLAB_LAKE)")

    src = Path(a.problem)
    if not src.exists():
        src = REPO / "FormalConjectures" / a.problem
    if not src.exists():
        sys.exit(f"REFUSING: no such problem file: {a.problem}")
    src = src.resolve()
    rel = str(src.relative_to(REPO / "FormalConjectures")) if src.is_relative_to(REPO / "FormalConjectures") else src.name
    orig = re.sub(r',?\s*formal_proof\s+using\s+[a-z0-9_]+\s+at\s+"[^"]*"', "", src.read_text())
    if not re.search(rf"\b{re.escape(a.theorem.split('.')[-1])}\b", orig):
        sys.exit(f"REFUSING: the theorem {a.theorem} does not appear in {src}")

    tag = re.sub(r"[^A-Za-z0-9]+", "_", rel.removesuffix(".lean") + "__" + a.theorem)
    base = (a.out or RUNS / tag).resolve()
    if (base / "result.json").exists():
        sys.exit(f"REFUSING: {base} already has a result.json")
    base.mkdir(parents=True, exist_ok=True)
    lib = base / "library"
    (lib / "verified").mkdir(parents=True, exist_ok=True)
    # unix socket paths are limited to about 107 bytes: the sockets live in a short directory keyed by the run
    svc = HOME / ".cache" / "lean-swarm" / hashlib.sha1(str(base).encode()).hexdigest()[:12]
    svc.mkdir(parents=True, exist_ok=True)
    log = open(base / "launch.log", "a")

    def say(msg: str):
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
        print(line, flush=True)
        log.write(line + "\n")
        log.flush()

    say(f"run {base}: {rel} :: {a.theorem}, {a.agents} agents, cap {a.cap} s, model {a.model}")

    # ---- the model relay (the wire meter, reachable from the jails through a unix socket)
    meter_port = free_port()
    meter = subprocess.Popen(
        [sys.executable, str(FRAMEWORK / "hermit" / "runtime" / "wire_meter.py")],
        env=dict(os.environ, PORT=str(meter_port), LEDGER=str(base / "wire_ledger.json"), UPSTREAM=binding.backend.origin,
                 REWRITE_DEVELOPER_ROLE="1" if binding.backend.developer_role == "system" else "0",
                 REWRITE_STRIP_SERVER_TOOLS="1", REWRITE_REPAIR_TOOL_ARGS="1"),
        stdout=open(base / "wire_meter.log", "a"), stderr=subprocess.STDOUT, start_new_session=True)
    relay_sock = svc / "relay.sock"
    if relay_sock.exists():
        relay_sock.unlink()
    relay = subprocess.Popen(["socat", f"UNIX-LISTEN:{relay_sock},fork,reuseaddr", f"TCP:127.0.0.1:{meter_port}"],
                             stdout=subprocess.DEVNULL, stderr=open(base / "relay.log", "a"), start_new_session=True)

    def meter_up():
        try:
            c = http.client.HTTPConnection("127.0.0.1", meter_port, timeout=2)
            c.request("GET", "/health")
            return c.getresponse().status == 200
        except OSError:
            return False
    wait_for(lambda: meter_up() and relay_sock.exists(), 30, "the model relay")

    # ---- workspaces and the service config
    from hermit.hosts.codex import prepare as codex_prepare

    ids = [f"A{i + 1}" for i in range(a.agents)]
    agents, svc_agents = {}, {}
    for me in ids:
        work = base / me
        work.mkdir(exist_ok=True)
        codex_prepare(ADAPTER, work, sys.executable)
        # the tools come from the team service (`mcp_servers.team` below), not from the per-session server deploy wrote
        (work / ".codex" / "config.toml").write_text("# Generated by hermit. The team tools come from the run's service.\n")
        (work / "Original.lean").write_text(orig)
        (work / "Problem.lean").write_text(orig)
        home = base / ".codex_homes" / me
        home.mkdir(parents=True, mode=0o700, exist_ok=True)
        prompt = PROMPT.format(work=work, rel=rel, name=a.theorem, repo=REPO, me=me,
                               others=", ".join(i for i in ids if i != me), lib=lib)
        (work / ".task_prompt.md").write_text(prompt)
        sock = svc / f"{me}.sock"
        key_env = binding.key_env_if_set()           # the backend's key variable, as codex reads it (none: no key)
        jail = agent_jail(work, home, ro=[lib], bridges=[(relay_sock, RELAY_PORT), (sock, TEAM_PORT)],
                          env={key_env: key or ""} if key_env else {})
        agents[me] = dict(work=work, home=home, cmd=jail + codex_cmd(work, binding, me, prompt), proc=None, launches=0,
                          status="running", last_graded=0.0, verdict=None, t_start=None, t_end=None, resumes=0,
                          exited_at=None, interruptions=0, t_interrupted=0.0, ev=None, er=None)
        svc_agents[me] = dict(socket=str(sock), audit_log=str(work / "tool-calls.jsonl"),
                              env={"COLLAB_WORKSPACE": str(work), "COLLAB_COMPILE_PREFIX": json.dumps(lean_jail_prefix(work))})
    control = str(svc / "control.sock")
    config = dict(adapter=str(ADAPTER), control_socket=control, tick=5, agents=svc_agents, log=str(base / "service.log"),
                  env={"COLLAB_DIR": str(lib), "COLLAB_TARGET": a.theorem, "COLLAB_REPO": str(REPO), "COLLAB_LAKE": LAKE})
    (base / "service_config.json").write_text(json.dumps(config, indent=1))

    def start_service() -> subprocess.Popen:
        for p in [control] + [s["socket"] for s in svc_agents.values()]:
            if os.path.exists(p):
                os.unlink(p)
        proc = subprocess.Popen([sys.executable, "-m", "hermit.runtime.service", "--config", str(base / "service_config.json")],
                                cwd=FRAMEWORK, stdout=open(base / "service.stdout", "a"), stderr=subprocess.STDOUT,
                                start_new_session=True)

        def up():
            if not all(os.path.exists(p) for p in [control] + [s["socket"] for s in svc_agents.values()]):
                return False
            try:
                ctl(control, "GET", "/health")
                return True
            except OSError:
                return False
        wait_for(up, 30, "the team service (see service.stdout)")
        return proc

    service = start_service()
    say(f"team service up: {len(ids)} endpoints under {svc}")

    judge = Judge(orig, a.theorem)
    (base / ".judge").mkdir(exist_ok=True)
    gpool = ThreadPoolExecutor(a.agents)

    def shutdown():
        for ag in agents.values():
            killpg(ag["proc"])
        killpg(service)
        killpg(relay)
        killpg(meter)
        gpool.shutdown(wait=False, cancel_futures=True)

    if a.dry_run:
        probe = agents["A1"]["cmd"][:agents["A1"]["cmd"].index("codex")] + [
            "bash", "-c", f"curl -s -o /dev/null -w 'relay %{{http_code}}' http://127.0.0.1:{RELAY_PORT}/health; echo; "
                          f"curl -s -o /dev/null -w 'team %{{http_code}}' -X POST -H 'Content-Type: application/json' "
                          f"http://127.0.0.1:{TEAM_PORT}/mcp/; echo; codex --version"]
        out = subprocess.run(probe, capture_output=True, text=True, timeout=60)
        say("jail probe: " + " / ".join(out.stdout.split()) + (f" (stderr: {out.stderr.strip()[-300:]})" if out.stderr.strip() else ""))
        t = time.time()
        judge.original(base / ".judge")
        say(f"judge: the original compiles in the Lean jail ({time.time() - t:.0f} s)")
        say("dry run: workspaces, service config and the service are in place; no agent started")
        shutdown()
        return

    # ---- the agents
    def launch(ag: dict, text: str | None = None):
        ag["launches"] += 1
        mode = "a" if text is not None else "w"
        for k in ("ev", "er"):
            if ag.get(k):
                ag[k].close()
        ag["ev"] = open(ag["work"] / "events.jsonl", mode)
        ag["er"] = open(ag["work"] / "stderr.log", mode)
        cmd = codex_resume_cmd(ag["cmd"], text) if text is not None else ag["cmd"]
        if text is not None:
            ag["resumes"] += 1
            ag["status"] = "running"
        ag["proc"] = subprocess.Popen(cmd, stdout=ag["ev"], stderr=ag["er"], stdin=subprocess.DEVNULL, start_new_session=True)
        if text is None:
            ag["t_start"] = time.time()

    def running(ag: dict) -> bool:
        return ag["status"] == "running" and ag["proc"] is not None and ag["proc"].poll() is None

    def stop(ag: dict, why: str):
        killpg(ag["proc"])
        if ag["status"] == "running":
            ag["status"] = why
            ag["t_end"] = time.time()

    t0 = time.time()
    for ag in agents.values():
        launch(ag)
    say("agents started")
    winner, t_solved, after, quiet_since, pending = None, None, 0, None, {}
    try:
        while True:
            now = time.time()
            for me, ag in agents.items():  # exits: a provider capacity error early on is retried, any other exit is reported
                if ag["status"] == "running" and ag["proc"].poll() is not None:
                    ag["ev"].flush()
                    if ("at capacity" in (ag["work"] / "events.jsonl").read_text(errors="replace")
                            and now - ag["t_start"] < 120 and ag["launches"] <= 3):
                        os.rename(ag["work"] / "events.jsonl", ag["work"] / f"events.capacity{ag['launches']}.jsonl")
                        time.sleep(30)
                        launch(ag)
                        continue
                    ag["status"] = f"exit {ag['proc'].returncode}"
                    ag["t_end"] = ag["exited_at"] = now
                    say(f"{me} exited ({ag['proc'].returncode})")
            for me, ag in agents.items():  # the judge: every changed file
                f = ag["work"] / "Problem.lean"
                mt = os.path.getmtime(f)
                if mt > ag["last_graded"] and me not in pending:
                    ag["last_graded"] = mt
                    gw = ag["work"] / ".grade"
                    gw.mkdir(exist_ok=True)
                    pending[me] = (gpool.submit(judge.grade, f.read_text(), gw), mt)
            for me in list(pending):
                if pending[me][0].done():
                    fut, mt = pending.pop(me)
                    v = fut.result()
                    agents[me]["verdict"] = v
                    say(f"{me}: {v}")
                    if v == "PROVED" and winner is None:
                        winner, t_solved = me, mt - t0
            if service.poll() is not None:
                say(f"team service exited ({service.returncode}); restarting from its state on disk")
                try:
                    service = start_service()
                    after = 0
                except RuntimeError as exc:
                    say(str(exc))
            status = dict(t0=t0, cap=a.cap, agents={
                me: dict(running=running(ag), t_start=ag["t_start"], t_interrupted=ag["t_interrupted"], resumes=ag["resumes"],
                         exited_at=ag["exited_at"], busy=agent_busy(ag["work"], now), grading=me in pending,
                         proved=ag["verdict"] == "PROVED", restarted=False) for me, ag in agents.items()})
            try:
                ctl(control, "POST", "/status", status)
                cmds = ctl(control, "GET", f"/commands?after={after}")["commands"]
            except OSError as exc:
                say(f"service: {exc}")
                cmds = []
            for c in cmds:  # the adapter's reactions, carried out as given
                after = max(after, c["seq"])
                ag = agents[c["agent"]]
                if c["kind"] == "interrupt":
                    if not running(ag):
                        res = "not running"
                    else:
                        stop(ag, "interrupted")
                        launch(ag, text=c["text"])
                        ag["resumes"] -= 1
                        ag["interruptions"] += 1
                        ag["t_interrupted"] = time.time()
                        res = "done"
                else:
                    if running(ag):
                        res = "already running"
                    else:
                        launch(ag, text=c["text"])
                        ag["exited_at"] = None
                        res = "done"
                say(f"{c['kind']} {c['agent']}: {res}")
                try:
                    ctl(control, "POST", "/result", dict(seq=c["seq"], id=c.get("id"), agent=c["agent"], kind=c["kind"], result=res))
                except OSError:
                    pass
            (base / "run_state.json").write_text(json.dumps(dict(
                t0=t0, heartbeat=now, cap_s=a.cap, winner=winner,
                agents={me: {k: ag.get(k) for k in ("t_start", "launches", "resumes", "interruptions", "status", "verdict")}
                        for me, ag in agents.items()}), indent=1))
            if winner:
                for me, ag in agents.items():
                    if me != winner:
                        stop(ag, "stopped: other agent solved")
                stop(agents[winner], "solved")
                break
            if now - t0 > a.cap:
                for ag in agents.values():
                    stop(ag, "time cap")
                break
            idle = all(ag["status"] != "running" for ag in agents.values()) and not pending
            quiet_since = (quiet_since or now) if idle else None
            if idle and now - quiet_since >= 15:  # three ticks with every agent ended and no resume from the adapter
                break
            time.sleep(POLL)
    except KeyboardInterrupt:
        say("interrupted")
        for ag in agents.values():
            stop(ag, "interrupted by the operator")

    # ---- the final grade: only where it can change the result
    futs = {}
    for me, ag in agents.items():
        mt = os.path.getmtime(ag["work"] / "Problem.lean")
        if me not in pending and ag["verdict"] is not None and mt <= ag["last_graded"]:
            continue
        if winner is not None and me != winner and mt - t0 >= t_solved:
            ag["verdict"] = ag["verdict"] or "not graded (written after the solve)"
            continue
        gw = ag["work"] / ".grade_final"
        gw.mkdir(exist_ok=True)
        futs[me] = gpool.submit(judge.grade, (ag["work"] / "Problem.lean").read_text(), gw)
    for me, ag in agents.items():
        if me in futs:
            ag["verdict"] = futs[me].result()
        ag["solve_s"] = round(os.path.getmtime(ag["work"] / "Problem.lean") - t0) if ag["verdict"] == "PROVED" else None
        if ag["verdict"] == "PROVED" and (winner is None or ag["solve_s"] < t_solved):
            winner, t_solved = me, ag["solve_s"]
    try:
        ledger = json.loads((base / "wire_ledger.json").read_text()).get("ledger", {}) if (base / "wire_ledger.json").exists() else {}
    except ValueError:
        ledger = {}
    shutdown()

    res = dict(problem=rel, theorem=a.theorem, model=a.model, n_agents=a.agents, cap_s=a.cap, solved=winner is not None,
               winner=winner, time_to_solve=round(t_solved) if t_solved is not None else None, wall=round(time.time() - t0), agents={})
    for me, ag in agents.items():
        tokens = ledger.get(me, {})
        res["agents"][me] = dict(status=ag["status"], launches=ag["launches"], resumes=ag["resumes"], interruptions=ag["interruptions"],
                                 wall=round((ag["t_end"] or time.time()) - (ag["t_start"] or t0)), verdict=ag["verdict"],
                                 solve_s=ag.get("solve_s"), tokens=tokens, cost_usd=cost(tokens, binding.price))
    res["n_proved"] = sum(1 for x in res["agents"].values() if x["verdict"] == "PROVED")
    costs = [x["cost_usd"] for x in res["agents"].values() if x["cost_usd"] is not None]
    res["cost_usd"] = round(sum(costs), 3) if costs else None
    if (lib / "state.json").exists():
        st = json.loads((lib / "state.json").read_text())
        res["team"] = dict(verified=[{k: e[k] for k in ("name", "by", "statement")} | {"t": round(e["t"] - t0)} for e in st["lemmas"].values()],
                           parts=[{k: g.get(k) for k in ("name", "status", "on")} for g in st["subgoals"].values()],
                           messages=len(st["messages"]),
                           pulls=sum(1 for e in st["events"] if e["kind"] == "pull"))
    (base / "result.json").write_text(json.dumps(res, indent=1))
    say(json.dumps({k: res[k] for k in ("problem", "theorem", "solved", "winner", "time_to_solve", "cost_usd")}))


if __name__ == "__main__":
    main()
