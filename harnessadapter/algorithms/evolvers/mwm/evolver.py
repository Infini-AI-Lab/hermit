"""The MWM Evolver: the native MetaWorld Model learner (V2 default: docket on, hypothesis off) on the Trainer.

One training step is one MWM block. The hosts of the block have already played (the Trainer's rollouts, through the
task's pipeline), and the task's evidence store holds their records in the native layout. The step then runs what
the native wrapper ran after a block:

1. maintenance on the adapter (journal roll, docket caps, gate advice, memory index; prompt.prepare_session);
2. one Codex session with the native session prompt (prompt.build_prompt), started in the adapter directory with the
   native mounts (the world model at /w, read-only at /world-model; /evidence, /environment, /skills, /patterns), in
   its own network namespace (only the
   model meter and a gate onto the venue's channels are forwarded; every session measures it first);
3. executable admission (prompt.finalize: every tool's own test, failures parked in tools/_rejected/);
4. the native retry: a session that produced nothing and did not time out is run once more.

A session that fails or times out leaves the adapter as it was before maintenance, as the native wrapper kept the
previous world model serving. The native bootstrap session (block 0) is the Evolver's `bootstrap`: the Trainer runs it
once before the first rollouts and commits it as step 0, so the first block is served by the bootstrapped adapter, as
natively; the evidence store it sees holds only its README.

After an adopted session the Evolver records which files changed (.mwm/edit_times.json, the catalog's ordering key in
place of per-file git commit times) and renders the host package into ADAPTER.md for hosts that read the context
file; the task's own pipeline renders the package live through interface.context instead.

Task protocol. A task opts in by returning, from its pipeline, RunOutput.extra["mwm"] = {"unit": "questions" |
"hands" | ..., "number": <the episode's number in the stream>, "entry": <its evidence entry, with the record
address>}. The step's goal is then the native per-block listing. The task's evidence store (native layout, README.md
at its root) is named by the `evidence` parameter and granted through evolver_access.environment; the venue's
venue texts (contract/<slot>.md, delegation, host.json, environment/) are the `deployment` directory
(default task-info/mwm/; see deployment.py for what the method fixes itself).

Parameters (evolver.params):
  model, args, home, price     as for the built-in codex Evolver (args carries the provider and reasoning settings)
  evidence                     the run's evidence store (required; must be among evolver_access.environment)
  deployment                   the venue's texts for MWM, relative to the run config (default: task-info/mwm)
  learner_tools                task tools the learner also gets as MCP tools (default: all); every task tool is served
                               through the gate either way (MW_TOOLSRV, for a venue's shell client)
  bootstrap                    run the bootstrap session in the first step (default true)
  session_timeout_s            per session (default 1800, the native evolver timeout)
  retry_wait_s                 wait before the infrastructure retry (default 30)
  block_size                   episodes per block (= data.step_size); when set, the stream's shorter final block gets
                               no session, as natively (learning ran only when learn_every episodes had gathered)
  network_isolation            the learner in its own network namespace, reaching only the tool endpoint and the
                               127.0.0.1 ports named in `args` (default true); every session measures it first
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

from harnessadapter.evolver.agent import parse_log
from harnessadapter.evolver.base import Context, Evolver, FinalResult, StepContext, StepResult, Usage

HERE = Path(__file__).resolve().parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"_mwm_evolver_{name}", HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module



WORK = "/w"                       # the learner's working directory: the world model, as natively mounted
EDIT_TIMES = ".mwm/edit_times.json"
# Contract rules 2 and 3: the wrapper's files and the code that serves the world model. A session that changes any
# of them is not adopted (the rules say the session is discarded; natively and in the old port it was).
PROTECTED = ("CONTRACT.md", "EVOLVER.md", "adapter.yaml", "interface.py", "check.py", "mwm_core/")
SKIP = ("__pycache__", ".git")
ACTIONS = ("command_execution", "file_change", "mcp_tool_call")     # codex items that act on something


def _copy(source: Path, target: Path) -> None:
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target, symlinks=True, ignore=shutil.ignore_patterns(*SKIP))


def _restore(snapshot: Path, adapter: Path) -> None:
    """Make `adapter` identical to `snapshot` (the directory itself stays: it is a bind mount). The adapter's git
    history (.git, from the Trainer) is not part of the snapshot and stays as it is."""
    for child in list(adapter.iterdir()):
        if child.name == ".git":
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    for child in snapshot.iterdir():
        if child.name == ".git":
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.copytree(child, adapter / child.name, symlinks=True)
        else:
            shutil.copy2(child, adapter / child.name, follow_symlinks=False)


def _digest(root: Path) -> dict[str, bytes]:
    out = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_file() and not any(part in SKIP for part in path.parts):
            out[rel] = path.read_bytes()
    return out


def _changed(before: dict, after: dict) -> list[str]:
    return sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))


class _Relay:
    """Unix socket -> host 127.0.0.1:port, one per forwarded port, for the learner's network namespace."""

    def __init__(self, directory: Path, ports: list[int]):
        import socket
        import threading
        self.servers = []
        directory.mkdir(parents=True, exist_ok=True)
        for port in ports:
            path = directory / str(port)
            if len(str(path)) > 107:
                raise ValueError(f"socket path too long for AF_UNIX: {path}")
            if path.exists():
                path.unlink()
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(path))
            server.listen(64)
            self.servers.append(server)

            def serve(server=server, port=port):
                while True:
                    try:
                        client, _ = server.accept()
                    except OSError:
                        return
                    try:
                        upstream = socket.create_connection(("127.0.0.1", port), timeout=30)
                        upstream.settimeout(None)
                    except OSError:
                        client.close()
                        continue
                    for a, b in ((client, upstream), (upstream, client)):
                        threading.Thread(target=self._pipe, args=(a, b), daemon=True).start()
            threading.Thread(target=serve, daemon=True).start()

    @staticmethod
    def _pipe(a, b) -> None:
        import socket
        try:
            while True:
                data = a.recv(65536)
                if not data:
                    break
                b.sendall(data)
        except OSError:
            pass
        finally:
            for x in (a, b):
                try:
                    x.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def close(self) -> None:
        for server in self.servers:
            try:
                server.close()
            except OSError:
                pass


from harnessadapter.evolver.gate import ToolGate as _ToolGate   # the venue's channels are the learner's only tools


class MwmEvolver(Evolver):
    supports_resume = False          # every MWM session is a fresh Codex session, as natively

    # ------------------------------------------------------------------ inputs
    def _state(self, ctx: Context) -> dict:
        path = ctx.workspace.state / "mwm.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def _save(self, ctx: Context, state: dict) -> None:
        (ctx.workspace.state / "mwm.json").write_text(json.dumps(state, indent=1) + "\n")

    @staticmethod
    def _material_steps(ctx: StepContext) -> list[Path]:
        root = ctx.workspace.learning_material
        return sorted(p for p in root.glob("step_*") if p.is_dir() and (p / "summary.json").is_file())

    def _block_records(self, ctx: StepContext) -> tuple[list[dict], int]:
        """This step's per-episode records, in stream order, and the tick (episodes played so far)."""
        steps = self._material_steps(ctx)
        upto = [s for s in steps if int(json.loads((s / "summary.json").read_text())["step"]) <= ctx.progress.step]
        tick = sum(len(json.loads((s / "summary.json").read_text())["tasks"]) for s in upto)
        current = ctx.workspace.learning_material / f"step_{ctx.progress.step:04d}"
        records = []
        if current.is_dir():
            for task in json.loads((current / "summary.json").read_text())["tasks"]:
                path = current / "tasks" / f"{''.join(c if c.isalnum() or c in '-_.' else '_' for c in task)}.json"
                records.append(json.loads(path.read_text()) if path.exists() else {"id": task})
        return records, tick

    @staticmethod
    def _goal(records: list[dict], tick: int) -> str:
        """The native per-block listing: "This block covers <unit> a-b; the current tick is T." and one entry per
        episode between rules of '='. A task without the protocol gets one generic entry per task."""
        entries, numbers, unit = [], [], "tasks"
        for i, r in enumerate(records):
            mwm = ((r.get("pipeline_extra") or {}).get("mwm")) or {}
            if mwm.get("entry"):
                entries.append((mwm.get("number", i + 1), mwm["entry"]))
                numbers.append(mwm.get("number", i + 1))
                unit = mwm.get("unit", unit)
            else:
                judge = r.get("judge") or {}
                text = [f"## Task {r.get('id')}", f"score: {judge.get('score')}"]
                if judge.get("feedback"):
                    text.append(f"feedback: {judge['feedback']}")
                if r.get("trajectory"):
                    text.append(f"raw trajectory: {r['trajectory']}")
                entries.append((i + 1, "\n".join(text)))
                numbers.append(i + 1)
        entries.sort(key=lambda e: e[0])
        first, last = (min(numbers), max(numbers)) if numbers else (0, 0)
        body = ["=" * 70]
        for _, entry in entries:
            body += [entry, "=" * 70]
        return f"This block covers {unit} {first}-{last}; the current tick is {tick}.\n\n" + "\n".join(body)

    # ---------------------------------------------------------------- session
    def _session_dir(self, ctx: StepContext, label: str) -> Path:
        session = ctx.workspace.scratch / label
        if session.exists():
            shutil.rmtree(session)
        (session / "control").mkdir(parents=True)
        deployment = self._deployment_dir(ctx)
        if deployment.is_dir():
            _copy(deployment, session / "deployment")
        return session

    def _evidence(self, ctx: StepContext, bootstrap: bool) -> Path:
        evidence = Path(ctx.params["evidence"]).resolve()
        if not bootstrap:
            return evidence
        # Native bootstrap ran before any episode: the store held only its README.
        empty = ctx.workspace.scratch / "bootstrap_evidence"
        empty.mkdir(parents=True, exist_ok=True)
        if (evidence / "README.md").is_file():
            shutil.copy2(evidence / "README.md", empty / "README.md")
        return empty

    @staticmethod
    def _channel_tools(ctx: StepContext) -> list[str]:
        """What the gate lets through: the task's tools (the venue's channels), never the Trainer's own."""
        return [name for name, tool in (ctx.tools or {}).items() if getattr(tool, "source", "") == "task"]

    def _mcp_tools(self, ctx: StepContext) -> list[str]:
        """Which channels the learner also sees as MCP tools (learner_tools; null: all of them). A venue whose channel
        is a shell client (/environment/bin/task-env) sets []: the gate still serves the client."""
        only = ctx.params.get("learner_tools")
        allowed = self._channel_tools(ctx)
        return allowed if only is None else [n for n in only if n in allowed]

    @staticmethod
    def _forward_ports(ctx: StepContext, gate_url: str | None) -> list[int]:
        """Host loopback ports the learner needs: its tool gate and any 127.0.0.1 URL in the agent arguments (the
        model meter). The Trainer's own tool endpoint is never forwarded."""
        import re
        from urllib.parse import urlparse
        ports = set()
        if gate_url:
            ports.add(urlparse(gate_url).port)
        for arg in ctx.params.get("args", []):
            ports.update(int(p) for p in re.findall(r"https?://(?:127\.0\.0\.1|localhost):(\d+)", str(arg)))
        return sorted(p for p in ports if p)

    def _command(self, ctx: StepContext, session: Path, evidence: Path, prompt_text: str, home: Path,
                 net: Path | None = None, gate_url: str | None = None) -> tuple:
        params = ctx.params
        adapter = ctx.workspace.adapter.resolve()
        only = self._mcp_tools(ctx)
        tools = [sys.executable, str(HERE / "learner_tools.py"), "--url", gate_url or "",
                 *[a for n in only for a in ("--only", n)]]
        codex = shutil.which("codex") or "codex"
        if not shutil.which("bwrap"):
            raise RuntimeError("the mwm Evolver needs bubblewrap (bwrap): the learner runs under the native mount names")
        # The session's working directory is /w, the native mount of the world model (mwm/cx_session.py), so that
        # `../evidence` and the other native relative paths resolve; /world-model is the read-only alias, as natively.
        codex_cmd = [codex, "exec", "--json", "--skip-git-repo-check", "--ignore-user-config",
                     "-s", "danger-full-access", "-C", WORK,
                     "-c", f"mcp_servers.evolver_tools.command={json.dumps(tools[0])}",
                     "-c", f"mcp_servers.evolver_tools.args={json.dumps(tools[1:])}",
                     "-c", "mcp_servers.evolver_tools.tool_timeout_sec=1800",
                     *(["-m", params["model"]] if params.get("model") else []), *params.get("args", []), prompt_text]
        env = dict(os.environ)
        env["CODEX_HOME"] = str(home)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[4]) + os.pathsep + env.get("PYTHONPATH", "")
        env["PATH"] = str(HERE / "bin") + os.pathsep + env.get("PATH", "")
        environment = session / "deployment" / "environment"
        if not environment.is_dir():
            environment.mkdir(parents=True)
        mounts = [sys.executable, str(HERE / "mounts.py"),
                  "--ro", f"{evidence}:/evidence", "--ro", f"{environment}:/environment",
                  "--ro", f"{session / 'method' / 'skills'}:/skills",
                  "--ro", f"{session / 'method' / 'patterns'}:/patterns",
                  "--bind", f"{adapter}:{WORK}", "--ro", f"{adapter}:/world-model",
                  *[a for path in self._hidden(ctx) for a in ("--hide", str(path))],
                  "--chdir", WORK]
        if net is not None:
            mounts += ["--netns", *[a for port in self._forward_ports(ctx, gate_url)
                                    for a in ("--forward", f"{port}={net / str(port)}")],
                       "--probe", str(net / "probe.json")]
        mounts.append("--")
        if gate_url:
            env["MW_TOOLSRV"] = gate_url                # the venue's channel client (/environment/bin/task-env)
        if ctx.sandbox is not None:
            wrapped, env = ctx.sandbox.wrap(codex_cmd, writable=[home, *([net] if net else [])], env=env, home=home)
            if "--" in wrapped and wrapped[0] == "bwrap":
                # The outer sandbox was built for codex (its installation is visible); run it through the
                # mount launcher inside that sandbox.
                cut = wrapped.index("--")
                return wrapped[:cut + 1] + mounts + wrapped[cut + 1:], env
            return mounts + wrapped, env
        return mounts + codex_cmd, env

    @staticmethod
    def _hidden(ctx: StepContext) -> list[Path]:
        """What the framework's sandbox shows and the native learner never saw: the Trainer's other workspace areas
        (learning material, task-info, history, state, scratch, output; the session's own texts reach the learner
        only through the mounts), the framework checkout's examples, tests and docs (task code and data), and this
        Evolver's maintenance code and raw materials (the native learner never saw the wrapper)."""
        ws = ctx.workspace
        repo = Path(__file__).resolve().parents[4]
        hidden = [ws.learning_material, ws.task_info, ws.history, ws.state, ws.scratch, ws.output,
                  repo / "examples", repo / "tests", repo / "docs", HERE / "maintenance", HERE / "materials"]
        # The task's own additions (`hide`): a benchmark package installed beside the framework holds its evaluator
        # and its test tasks, and the sandbox shows the whole Python installation. "module:<name>" names one.
        for item in ctx.params.get("hide") or []:
            item = str(item)
            if item.startswith("module:"):
                spec = importlib.util.find_spec(item[len("module:"):])
                if spec is None or not spec.submodule_search_locations:
                    raise RuntimeError(f"hide: no installed package named {item[len('module:'):]!r}")
                hidden += [Path(loc) for loc in spec.submodule_search_locations]
            else:
                hidden.append(Path(item).expanduser())
        return hidden

    def _run(self, command: list[str], env: dict, ctx: StepContext, log: Path, cwd: Path, timeout: float) -> dict:
        start = time.time()
        timed_out = False
        with open(log, "w") as out:
            proc = subprocess.Popen(command, stdout=out, stderr=subprocess.STDOUT, env=env, cwd=str(cwd),
                                    start_new_session=True)
            while proc.poll() is None:
                cancelled = ctx.cancel is not None and ctx.cancel.is_set()
                timed_out = time.time() - start > timeout
                if cancelled or timed_out:
                    os.killpg(proc.pid, signal.SIGTERM)
                    try:
                        proc.wait(15)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL)
                    break
                time.sleep(1)
            code = proc.wait()
        return {"exit_code": code, "timed_out": timed_out, "wall_s": round(time.time() - start, 1),
                "cancelled": bool(ctx.cancel is not None and ctx.cancel.is_set())}

    def _home(self, ctx: Context) -> Path:
        """A throwaway CODEX_HOME per session, as natively (mwm/cx_session.py): the credentials named by `home`,
        none of the previous sessions' history (Codex keeps its transcripts there)."""
        home = ctx.workspace.state.parent / "agent_home"
        if home.exists():
            shutil.rmtree(home)
        home.mkdir(parents=True)
        source = Path.home() / ".codex"
        for name in ctx.params.get("home", []):
            if (source / name).is_file() and not (home / name).exists():
                shutil.copy2(source / name, home / name)
                os.chmod(home / name, 0o600)
        return home

    def _one_session(self, ctx: StepContext, *, block: int, tick: int, goal: str, notes: str, label: str) -> dict:
        """Maintenance, one Codex session, admission. Restores the adapter when the session is not adopted."""
        adapter = ctx.workspace.adapter
        snapshot = ctx.workspace.scratch / f"{label}_before"
        _copy(adapter, snapshot)
        before = _digest(adapter)
        session = self._session_dir(ctx, label)
        record: dict = {"label": label, "block": block, "tick": tick}
        try:
            state = self._isolated(ctx, "prepare", adapter=str(adapter), session=str(session), block=block, tick=tick,
                                   evidence=str(Path(ctx.params["evidence"])), goal=goal, notes=notes)
            text = state["prompt"]
        except Exception as exc:                               # noqa: BLE001
            _restore(snapshot, adapter)
            record.update(status="prepare_failed", error=f"{type(exc).__name__}: {exc}")
            return record
        (ctx.workspace.output / f"{label}.prompt.md").write_text(text)
        prepared = _digest(adapter)                        # after maintenance: what the session is handed
        record["advice"] = state.get("advice")
        home = self._home(ctx)
        # The forward sockets need a short path (AF_UNIX); /tmp is private inside the sandbox, so the directory is
        # bound in explicitly.
        net = Path(tempfile.mkdtemp(prefix="mwm-", dir="/tmp")) if ctx.params.get("network_isolation", True) else None
        gate = _ToolGate(ctx.tool_url, self._channel_tools(ctx)) if ctx.tool_url else None
        gate_url = gate.url if gate else None
        command, env = self._command(ctx, session, self._evidence(ctx, block == 0), text, home, net, gate_url)
        log = ctx.workspace.output / f"{label}.codex.jsonl"
        relay = _Relay(net, self._forward_ports(ctx, gate_url)) if net is not None else None
        try:
            run = self._run(command, env, ctx, log, adapter, float(ctx.params.get("session_timeout_s", 1800)))
        finally:
            if relay is not None:
                relay.close()
            if gate is not None:
                gate.close()
        if net is not None:
            probe = net / "probe.json"
            record["isolation"] = json.loads(probe.read_text()) if probe.exists() else {"passed": False,
                                                                                         "error": "no probe result"}
            shutil.rmtree(net, ignore_errors=True)
        session_id, usage = parse_log(log, "codex", ctx.params.get("model") or "codex")
        events = [json.loads(l) for l in log.read_text(errors="replace").splitlines() if l.startswith("{")] \
            if log.exists() else []
        record.update(run, session=session_id, usage=[u.__dict__ for u in usage],
                      turns=sum(1 for e in events if e.get("type") == "turn.completed"),
                      tool_calls=sum(1 for e in events if e.get("type") == "item.completed"
                                     and (e.get("item") or {}).get("type") in ACTIONS))
        record["_usage"] = usage
        # The old port's adoption rule: a clean exit, no timeout, and at least one action taken (it required a
        # successful broker call; a session that only wrote a message is not adopted).
        if run["exit_code"] != 0 or run["timed_out"] or run["cancelled"] or not record["tool_calls"]:
            _restore(snapshot, adapter)
            record["status"] = ("timed_out" if run["timed_out"] else "cancelled" if run["cancelled"]
                                else "isolation_failed" if run["exit_code"] == 97 else
                                "failed" if run["exit_code"] != 0 else "no_action")
            record["changed"] = []
            return record
        broken = [rel for rel in _changed(prepared, _digest(adapter))
                  if any(rel == p or (p.endswith("/") and rel.startswith(p)) for p in PROTECTED)]
        if broken:
            _restore(snapshot, adapter)
            record.update(status="rule_broken", changed=[], protected_changes=broken)
            return record
        try:
            admission = self._isolated(ctx, "admit", adapter=str(adapter), session=str(session))
        except Exception as exc:                               # noqa: BLE001 - a worker/isolation failure in admission
            _restore(snapshot, adapter)
            record.update(status="admit_failed", error=f"{type(exc).__name__}: {exc}", changed=[])
            return record
        record["admission"] = admission
        changed = _changed(before, _digest(adapter))
        record.update(status="adopted", changed=changed)
        self._record_edits(adapter, changed)
        return record

    def _session(self, ctx: StepContext, *, label: str, **kw) -> dict:
        """The native retry: once more after an infrastructure failure (nothing produced, no timeout)."""
        record = self._one_session(ctx, label=label, **kw)
        # Retried: a session that took no action and did not time out (the old port: no successful broker call).
        if record["status"] in ("failed", "no_action", "prepare_failed") and not record.get("timed_out") \
                and not record.get("tool_calls"):
            time.sleep(float(ctx.params.get("retry_wait_s", 30)))
            retry = self._one_session(ctx, label=label + "_retry", **kw)
            retry["retried_after"] = record["status"]
            retry["_usage"] = record.get("_usage", []) + retry.get("_usage", [])
            record = retry
        return record

    # ------------------------------------------------------------- outputs
    @staticmethod
    def _record_edits(adapter: Path, changed: list[str]) -> None:
        """The catalog's ordering key: the time of the session that last changed each file."""
        path = adapter / EDIT_TIMES
        times = {}
        if path.is_file():
            try:
                times = json.loads(path.read_text())
            except ValueError:
                times = {}
        now = time.time_ns()
        for rel in changed:
            if rel == EDIT_TIMES:
                continue
            if (adapter / rel).is_file():
                times[rel] = now
            else:
                times.pop(rel, None)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(times, indent=1, sort_keys=True) + "\n")

    @staticmethod
    def _deployment_dir(ctx: Context) -> Path:
        """The venue's texts for MWM: `deployment` (relative to the run config), else task-info/mwm (or the older
        task-info/deployment)."""
        from harnessadapter.algorithms.evolvers.mwm import deployment as _dep
        value = ctx.params.get("deployment")
        if not value:
            return _dep.find(ctx.workspace.task_info) or ctx.workspace.task_info / "mwm"
        path = Path(value)
        if not path.is_absolute():
            path = (ctx.base or Path.cwd()) / path
        return path.resolve()

    def _render_context(self, ctx: Context) -> None:
        """ADAPTER.md: the host package rendered with this deployment, for hosts that read the context file."""
        self._isolated(ctx, "render", adapter=str(ctx.workspace.adapter), deployment=str(self._deployment_dir(ctx)))

    @staticmethod
    def _isolated(ctx: Context, name: str, **arguments):
        """Run a maintenance step that executes the adapter's modules (isolated.py) in the learner's sandbox; in
        this process only when the run has no sandbox."""
        if ctx.sandbox is None or not getattr(ctx.sandbox, "enabled", True):
            module = _load("isolated")
            return getattr(module, name)(**arguments)
        from harnessadapter.isolate import _run_worker
        from harnessadapter.sandbox import framework_env
        work = ctx.workspace.scratch / f"maintenance-{uuid.uuid4().hex[:8]}"
        (work / "home").mkdir(parents=True)
        readable = [HERE]
        if name == "render":
            readable.append(Path(arguments["deployment"]))
        job = {"kind": "tool", "entrypoint": f"{HERE / 'isolated.py'}:{name}", "base": str(HERE),
               "arguments": arguments, "bound": {}}
        timeout = float(ctx.params.get("session_timeout_s", 1800))
        try:
            return _run_worker(ctx.sandbox, job, work, home=work / "home", readable=readable,
                               writable=[Path(arguments["adapter"]), *([Path(arguments["session"])]
                                                                      if "session" in arguments else [])],
                               env=framework_env(), timeout=timeout)["result"]
        finally:
            shutil.rmtree(work, ignore_errors=True)

    @staticmethod
    def _notes(admission: dict | None) -> str:
        """Native wrapper notes: executables parked because their own test failed."""
        parked = [t for t in ((admission or {}).get("tools") or []) if t.get("parked_at")]
        if not parked:
            return ""
        return ("Executables parked by the wrapper (their own test failed; already moved to _rejected/):\n"
                + "\n".join(f"- {t['tool']}: {str(t.get('output', ''))[-300:]}" for t in parked))

    # ------------------------------------------------------------------ steps
    def setup(self, ctx: Context) -> None:
        if not ctx.params.get("evidence"):
            raise ValueError("the mwm Evolver needs params.evidence: the task's evidence store")
        evidence = Path(ctx.params["evidence"]).resolve()
        granted = [Path(p).resolve() for p in ctx.access.environment]
        if not any(evidence == g or evidence.is_relative_to(g) for g in granted):
            raise ValueError(f"params.evidence ({evidence}) must be granted through evolver_access.environment")
        if not self._deployment_dir(ctx).is_dir():
            raise ValueError("the mwm Evolver needs the venue's texts: params.deployment, or task-info/mwm/")
        self._save(ctx, {"notes": "", "sessions": []})

    def step(self, ctx: StepContext) -> StepResult:
        state = self._state(ctx)
        records, tick = self._block_records(ctx)
        block = ctx.progress.step
        size = ctx.params.get("block_size")
        if size and ctx.progress.is_epoch_end and len(records) < int(size):
            # Native: a learner session runs when the episodes since the last one reach learn_every; the stream's
            # last, shorter block is played but never reviewed.
            (ctx.workspace.output / "report.md").write_text(f"MWM block {block}: partial final block, no session\n")
            return StepResult("done", f"MWM block {block}: {len(records)} of {size} episodes, partial final "
                                      "block, no session (native)")
        sessions = []
        usage: list[Usage] = []
        review = self._session(ctx, block=block, tick=tick, goal=self._goal(records, tick),
                               notes=state.get("notes", ""), label=f"review_{block:04d}")
        usage += review.pop("_usage", [])
        sessions.append(review)
        state["notes"] = self._notes(review.get("admission"))      # native: from the last session, every block
        return self._close(ctx, state, sessions, usage, f"MWM block {block}")

    def bootstrap(self, ctx: StepContext) -> StepResult | None:
        """The native bootstrap session (block 0), before any episode: the Trainer calls it once, before the first
        rollouts, and commits the adopted adapter as step 0. The evidence store it sees holds only its README."""
        if not ctx.params.get("bootstrap", True):
            return None
        state = self._state(ctx)
        boot = self._one_session(ctx, block=0, tick=0, goal="No episodes have been played yet.", notes="",
                                 label="bootstrap")
        if boot["status"] != "adopted":
            # Native rule: one retry on any failure, then the run continues from the seed.
            retry = self._one_session(ctx, block=0, tick=0, goal="No episodes have been played yet.", notes="",
                                      label="bootstrap_again")
            retry["_usage"] = boot.get("_usage", []) + retry.get("_usage", [])
            boot = retry
        usage = boot.pop("_usage", [])
        return self._close(ctx, state, [boot], usage, "MWM bootstrap")

    def _close(self, ctx: StepContext, state: dict, sessions: list[dict], usage: list, title: str) -> StepResult:
        """Render ADAPTER.md after an adopted session, save the state and the records, and hand back the result."""
        adopted = [s for s in sessions if s["status"] == "adopted"]
        if adopted:
            try:
                self._render_context(ctx)
            except Exception as exc:                          # noqa: BLE001
                sessions.append({"label": "render", "status": "failed", "error": f"{type(exc).__name__}: {exc}"})
        state.setdefault("sessions", []).extend({k: v for k, v in s.items() if k != "admission"} for s in sessions)
        self._save(ctx, state)
        (ctx.workspace.output / "sessions.json").write_text(json.dumps(sessions, indent=1, default=str))
        merged = Usage(model=ctx.params.get("model") or "codex")
        for u in usage:
            merged.input_tokens += u.input_tokens
            merged.cached_input_tokens += u.cached_input_tokens
            merged.output_tokens += u.output_tokens
            merged.calls += u.calls
        price = ctx.params.get("price")
        if price and merged.calls:
            merged.cost_usd = ((merged.input_tokens - merged.cached_input_tokens) * price.get("input", 0)
                               + merged.cached_input_tokens * price.get("cached_input", price.get("input", 0))
                               + merged.output_tokens * price.get("output", 0)) / 1e6
        summary = "; ".join(f"{s['label']}: {s['status']}" + (f" ({len(s.get('changed') or [])} files)"
                                                              if s["status"] == "adopted" else "") for s in sessions)
        (ctx.workspace.output / "report.md").write_text(f"{title}: {summary}\n")
        return StepResult("done" if adopted else "failed", f"{title}: {summary}",
                          usage=[merged] if merged.calls else [],
                          extra={"sessions": [{k: v for k, v in s.items() if k not in ("admission", "advice")}
                                              for s in sessions]})

    def finalize(self, ctx: Context) -> FinalResult:
        home = ctx.workspace.state.parent / "agent_home"
        for name in ctx.params.get("home", []):
            if (home / name).is_file():
                (home / name).unlink()
        return FinalResult(adapter=ctx.workspace.adapter, summary="the adapter after the last block")
