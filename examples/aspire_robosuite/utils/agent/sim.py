"""The robot execution engine: ASPIRE's CaP harness (github.com/NVlabs/ASPIRE, `cap-harness`) in its own venv.

Every call here is a subprocess of ASPIRE's `.venv-robosuite`; nothing of ASPIRE is imported into this process. A
program runs as `cap-harness run-batch` over a list of seeds, and each seed leaves ASPIRE's run directory
(docs/run-artifacts.md): outcome.json, source/program-result.json (one CallTrace per public call), trace/events.jsonl
and media/keyframes/ (lossless RGB before and after every motion call). `digest` reads those back as the per-primitive
trace the actor diagnoses from.

A generated program runs inside cap-harness, and ASPIRE's validator is no boundary: every cap-harness process (and the
validator's) runs in a bubblewrap sandbox of its own (`confine`), which reads ASPIRE's checkout, writes only the run's
own output, reaches only the perception, IK and planning services' loopback ports (ASPIRE's, or the URLs in
CAP_HARNESS_SAM3_URL / CAP_HARNESS_CUROBO_URL), and renders on the GPUs ASPIRE_SIM_GPU names (the N of
/dev/nvidia<N>; inside they count from 0, so MUJOCO_EGL_DEVICE_ID stays 0), in software without one. The rest of
the render environment is the config's (`agent.params.sim_env`, e.g. MUJOCO_GL=egl). See the example's README.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time
from urllib.parse import urlsplit

# The motion tools around which ASPIRE records before/after keyframes (cap_harness/runtime.py).
MOTION_TOOLS = ("step", "execute_trajectory", "set_gripper", "set_grippers", "move_to_joints", "move_to_pose",
                "move_synchronized", "open_gripper", "close_gripper", "go_home")

_VALIDATE = """
import ast, sys
from cap_harness.runtime import _ProgramValidator
src = open(sys.argv[1]).read()
try:
    _ProgramValidator().visit(ast.parse(src))
except (SyntaxError, ValueError) as exc:
    print(f"{type(exc).__name__}: {exc}")
    sys.exit(1)
"""


# The environment's secrets (model keys, tokens), which cap-harness does not need and a generated program must not
# reach: it runs in cap-harness's process, on the host
SECRET = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH", re.I)


def scrubbed(environ) -> dict:
    """environ without its secrets, for the simulator's processes."""
    return {k: v for k, v in environ.items() if not SECRET.search(k)}


# The services' loopback ports a program reaches from its sandbox, ASPIRE's own (cap_harness/providers/*/client.py):
# segmentation, Contact-GraspNet, PyRoki and cuRobo. A CAP_HARNESS_*_URL in the environment that moves one is
# forwarded as well; one on another host goes through an allowlist proxy that reaches only it.
PROVIDER_PORTS = (8114, 8115, 8116, 8118)
# What of ASPIRE's checkout a program never sees: its worked solutions, its agents' skills and prompts, its tests,
# the runs ASPIRE's own CLI leaves (outputs/, their programs' sources), and its git history, which holds them all
# (cap-harness runs from src/, third_party/, assets/, configs/ and the venvs; without git it records no commit in a
# run's provenance)
HIDDEN = ("examples", ".claude", ".agents", ".codex", "tests", "outputs", ".git")


def gpus_of(value) -> list[int]:
    """`"8,9"`, `[8, 9]` or None -> the GPUs the simulator renders on (the N of /dev/nvidia<N>)."""
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple)):
        return [int(v) for v in value]
    return [int(v) for v in str(value).replace(",", " ").split()]


def render_paths(env: dict) -> list[Path]:
    """What the renderer reads from outside the system's directories, file by file: an NVIDIA EGL vendor library
    extracted by hand (the ICD files __EGL_VENDOR_LIBRARY_FILENAMES names, the library each names, as GLVND finds
    it: absolute, beside the ICD when it has a slash, else on LD_LIBRARY_PATH, and the NVIDIA libraries beside it,
    which it loads), not the directories they lie in; and what ASPIRE_SIM_READ lists (paths separated by `:`, the
    operator's explicit grants)."""
    out = []
    search = [Path(d) for d in str(env.get("LD_LIBRARY_PATH") or "").split(":") if d]
    for icd in filter(None, str(env.get("__EGL_VENDOR_LIBRARY_FILENAMES") or "").split(":")):
        out.append(Path(icd))
        try:
            name = str(json.loads(Path(icd).read_text())["ICD"]["library_path"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        candidates = [Path(name)] if name.startswith("/") else [Path(icd).parent / name] if "/" in name \
            else [d / name for d in search]
        library = next((c for c in candidates if c.is_file()), None)
        if library is not None:                 # a bare name not on LD_LIBRARY_PATH is the system's (/usr)
            out += [library, *sorted(library.parent.glob("libnvidia-*.so*")),
                    *sorted(library.parent.glob("libEGL_nvidia*"))]
    out += [Path(p) for p in str(env.get("ASPIRE_SIM_READ") or "").split(":") if p]
    return [p for p in dict.fromkeys(out) if p.exists()]


class _Confined:
    """What a confined command holds open: its sandbox and, for a provider on another host, the allowlist proxy."""

    def __init__(self, *held):
        self.held = held

    def close(self) -> None:
        for item in self.held:
            item.close()


def confine(cmd: list[str], *, root: Path, env: dict, writable=(), readable=(), gpu=(), hidden=(), record_dir=None):
    """`cmd` in a bubblewrap sandbox of its own: (what to close once the command ended; its command line; its
    environment). It reads ASPIRE's checkout at `root` (less HIDDEN; `hidden`, the run storage, and `writable` must
    lie outside it) and `readable` (the run's own, untrusted: nothing in it widens the sandbox), writes only
    `writable`, reaches only the services, and sees only the GPUs `gpu` names."""
    from hermit.sandbox import AllowlistProxy, Sandbox
    root = Path(root)
    first = Path(cmd[0])
    if (first.parent.parent / "pyvenv.cfg").is_file():
        # A venv's interpreter (the validator's `python -c`): the sandbox runs a command by its real path, and the
        # venv's python is a link to its base installation, which would run without the venv's packages; run by its
        # own name through a shell, the venv (granted, so its base installation is shown too) is kept.
        trusted = [first.parent.parent]
        cmd = ["/bin/sh", "-c", 'exec "$0" "$@"', *cmd]
    else:
        trusted = []
    # Only the services: not every loopback URL in the environment (the judge runs in hermit's own process, whose
    # tool endpoint and model meter are on the loopback too)
    urls = [urlsplit(str(v)) for k, v in env.items() if k.startswith("CAP_HARNESS_") and k.endswith("_URL") and v]
    loopback = [u for u in urls if u.hostname in ("127.0.0.1", "localhost")]
    ports = {*PROVIDER_PORTS, *(u.port or (443 if u.scheme == "https" else 80) for u in loopback)}
    remote = sorted({u.hostname for u in urls if u.hostname and u not in loopback})
    inside = [str(p) for p in [*hidden, *writable] if Path(p).resolve().is_relative_to(root.resolve())]
    if inside:      # covering run storage there would cover the run's own program and output too
        raise ValueError(f"{', '.join(inside)} lies inside ASPIRE's checkout {root}, which the simulator reads: "
                         "keep the runs elsewhere (ASPIRE_OUT, the direct actor's scratch)")
    proxy = AllowlistProxy(remote) if remote else None
    box = Sandbox(enabled=True, network="allowlist" if remote else "no-network", allow=remote,
                  proxy_url=proxy.url if proxy else None, readable=[root, *render_paths(env), *trusted],
                  writable=[Path(w) for w in writable], framework=False, gpu=list(gpu), forward=sorted(ports),
                  record_dir=Path(record_dir) if record_dir else None, role="cap-harness")
    box.hide = [*box.hide, *(root / h for h in HIDDEN)]
    held = _Confined(box, *([proxy] if proxy else []))
    try:
        # The run's own paths (the session's program) per call: the sandbox's own grants are trusted (a venv's
        # pyvenv.cfg there names an installation to show), and a program may be named pyvenv.cfg
        # Without a GPU, EGL must find Mesa's software renderer: an NVIDIA-only ICD selection would find no device
        drop = {"ASPIRE_SIM_URL", *([] if gpu else ["__EGL_VENDOR_LIBRARY_FILENAMES"])}
        args, env = box.wrap(list(cmd), env={k: v for k, v in env.items() if k not in drop}, chdir=root,
                             env_ports=False, readable=[Path(r) for r in readable])
    except BaseException:
        held.close()
        raise
    return held, args, env


class Engine:
    """ASPIRE's harness at `root` (a checkout with `.venv-robosuite` bootstrapped)."""

    def __init__(self, root: Path | str, *, env: dict | None = None, workers: int = 4, max_steps: int | None = None,
                 timeout_s: float = 3600.0, gpu=None):
        self.root = Path(root).expanduser().resolve()
        self.cli = self.root / ".venv-robosuite" / "bin" / "cap-harness"
        self.python = self.root / ".venv-robosuite" / "bin" / "python"
        if not self.cli.is_file():
            raise FileNotFoundError(f"{self.cli}: bootstrap ASPIRE's Robosuite venv (scripts/bootstrap_robosuite.sh)")
        self.env = {k: str(v) for k, v in (env or {}).items()}
        self.workers, self.max_steps, self.timeout_s = int(workers), max_steps, float(timeout_s)
        self.gpu = gpus_of(os.environ.get("ASPIRE_SIM_GPU") if gpu is None else gpu)
        self.hidden = [Path(os.environ["ASPIRE_OUT"])] if os.environ.get("ASPIRE_OUT") else []   # the runs, Stage 2

    def _env(self) -> dict:
        env = scrubbed(os.environ)
        for key, value in self.env.items():
            env[key] = os.path.expandvars(os.path.expanduser(value))
        return env

    def validate(self, program: Path) -> str | None:
        """The sandbox's own static check of a program (ASPIRE's _ProgramValidator): None, or why it is refused."""
        box, args, env = confine([str(self.python), "-c", _VALIDATE, str(program)], root=self.root, env=self._env(),
                                 readable=[Path(program).parent], gpu=[], hidden=self.hidden)
        try:
            done = subprocess.run(args, capture_output=True, text=True, cwd=self.root, env=env, timeout=300)
        except subprocess.TimeoutExpired:
            return None   # the check could not run in time: the sweep's own validation still refuses a bad program
        finally:
            box.close()
        return None if done.returncode == 0 else (done.stdout.strip() or done.stderr.strip()[-2000:])

    def run(self, program: Path, *, suite: str, seeds: list[int], out: Path, task_id: int = 0,
            videos: bool = False) -> list[dict]:
        """Run `program` on every seed; one record per seed: seed, task_success, program_ok, termination_reason,
        run_dir, error (ASPIRE's results.jsonl joined with each run's outcome.json)."""
        out.mkdir(parents=True, exist_ok=True)
        results = out / "results.jsonl"
        results.unlink(missing_ok=True)
        cmd = [str(self.cli), "run-batch", "--benchmark", "robosuite", "--suite", suite, "--task-id", str(task_id),
               "--seeds", *[str(s) for s in seeds], "--program", str(program), "--output-root", str(out),
               "--flat-run-dir", "--workers", str(min(self.workers, len(seeds))), "--results-jsonl", str(results),
               "--log-dir", str(out / "logs")]
        if not videos:
            cmd.append("--no-videos")
        if self.max_steps:
            cmd += ["--max-steps", str(self.max_steps)]
        started = time.time()
        # run-batch's workers are its children: a timeout (or an interrupt) stops the whole process group, so no
        # simulator outlives its sweep
        box, args, env = confine(cmd, root=self.root, env=self._env(), writable=[out], readable=[Path(program).parent],
                                 gpu=self.gpu, hidden=self.hidden)
        log_path = out.parent / f"{out.name}.run-batch.log"       # beside the run: no program's to replace
        try:
            with open(log_path, "w") as log:
                proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=log, cwd=self.root, env=env,
                                        start_new_session=True)
                try:
                    proc.wait(timeout=self.timeout_s)
                except BaseException:
                    _stop(proc)
                    if not isinstance(sys.exc_info()[1], subprocess.TimeoutExpired):
                        raise
                    log.write(f"\nrun-batch timed out after {self.timeout_s:.0f} s\n")
        finally:
            box.close()
        # The program could write anything under `out` while it ran: what is read here is a regular file inside it,
        # never through a link it left (it has ended, and its sandbox with it)
        _sanitize(out)
        tail = log_path.read_text(errors="replace")[-4000:]
        rows = {}
        if results.is_file():
            for line in results.read_text(errors="replace").splitlines():
                try:
                    row = json.loads(line)
                    rows[int(row["seed"])] = row
                except (ValueError, KeyError, TypeError):
                    continue   # a line cut short when the sweep was stopped
        records = []
        for seed in seeds:
            row = rows.get(int(seed))
            record = {"seed": int(seed), "task_success": False, "program_ok": False, "termination_reason": None,
                      "run_dir": None, "error": None}
            if row is None:
                record["error"] = f"no result for seed {seed}: {tail[-600:]}"
            else:
                inside = row.get("run_dir") and Path(str(row["run_dir"])).resolve().is_relative_to(out.resolve())
                record["run_dir"] = row.get("run_dir") if inside else None
                record["error"] = row.get("error") if inside or not row.get("run_dir") else \
                    f"run_dir {row['run_dir']} is not in this run's output"
                outcome = _read_json(Path(row["run_dir"]) / "outcome.json") if inside else None
                if outcome:
                    record.update(task_success=bool(outcome.get("task_success")),
                                  program_ok=bool(outcome.get("program_ok")),
                                  termination_reason=outcome.get("termination_reason"),
                                  steps_executed=outcome.get("steps_executed"),
                                  error=outcome.get("error") or record["error"])
            records.append(record)
        (out / "records.json").write_text(json.dumps({"wall_s": time.time() - started, "records": records}, indent=1))
        return records


def seeds_of(value) -> list[int]:
    """`[1, 2]`, `"101-125"`, `"1-3 7"` -> a list of ints."""
    if value is None:
        return []
    if isinstance(value, int):
        return [value]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in seeds_of(v)]
    out = []
    for part in str(value).replace(",", " ").split():
        lo, _, hi = part.partition("-")
        out += list(range(int(lo), int(hi) + 1)) if hi else [int(lo)]
    return out


def _stop(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
    except ProcessLookupError:
        pass


def _sanitize(root: Path) -> None:
    """Every link, pipe or device under `root` removed: what a confined program left there is read as files. Each
    directory is made listable first (a program could make one unlistable to keep a link in it), and a directory
    that still cannot be listed fails the run rather than being passed over."""
    from hermit.sandbox import owner_readable
    owner_readable(root)

    def fail(error):
        raise error
    for where, dirs, files in os.walk(root, topdown=False, onerror=fail):
        for name in [*dirs, *files]:
            path = os.path.join(where, name)
            mode = os.lstat(path).st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                os.unlink(path)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def calls(run_dir: Path | str | None) -> list[dict]:
    """The program's public calls in order (ASPIRE's CallTrace records)."""
    if not run_dir:
        return []
    result = _read_json(Path(run_dir) / "source" / "program-result.json") or {}
    return list(result.get("calls") or [])


def program_result(run_dir: Path | str | None) -> dict:
    if not run_dir:
        return {}
    data = _read_json(Path(run_dir) / "source" / "program-result.json") or {}
    return {k: data.get(k) for k in ("ok", "error", "result", "stdout")}


def _clip(text, n: int) -> str:
    text = text if isinstance(text, str) else json.dumps(text, default=str)
    return text if len(text) <= n else text[: n // 2] + " ... " + text[-n // 2:]


# Observations repeat the whole camera state on every call; the trace names them only.
ELIDED = ("Observation", "CameraObservation", "TaskContext")
PROVIDERS = ("sam3-", "pyroki-", "curobo-", "contact-graspnet-", "graspnet-")


def compact(value, digits: int = 4, max_items: int = 16):
    """A call's recorded JSON made readable: inline arrays as rounded lists (a long one as its shape), floats
    rounded, `$type` kept as `type`."""
    if isinstance(value, dict):
        if "$array_inline" in value:
            items = value["$array_inline"]
            flat = items if not items or not isinstance(items[0], list) else [x for row in items for x in row]
            if len(flat) > max_items:
                return f"<array {value.get('dtype')} shape {value.get('shape')}>"
            return compact(items, digits, max_items)
        if "$array" in value or "$array_file" in value:
            return f"<array {value.get('dtype')} shape {value.get('shape')}>"
        if value.get("$type") in ELIDED:
            return f"<{value['$type']}>"
        out = {}
        for key, item in value.items():
            out["type" if key == "$type" else key] = compact(item, digits, max_items)
        return out
    if isinstance(value, list):
        if len(value) > 4 * max_items:
            return [compact(v, digits, max_items) for v in value[:max_items]] + [f"... {len(value) - max_items} more"]
        return [compact(v, digits, max_items) for v in value]
    if isinstance(value, float):
        return round(value, digits)
    return value


def digest(record: dict, *, call_chars: int = 500, max_calls: int = 80) -> str:
    """One seed's per-primitive trace as text: the outcome, then every recorded call in order (the program's public
    calls, and indented below them the provider calls they made) with its inputs, its output or error and its
    return status; the failing calls at three times the width; then the program's result and stdout."""
    lines = [f"seed {record['seed']}: task_success={record['task_success']} program_ok={record['program_ok']} "
             f"termination={record.get('termination_reason')} steps={record.get('steps_executed')}"]
    if record.get("error"):
        lines.append(f"  error: {_clip(record['error'], 1200)}")
    run_dir = record.get("run_dir")
    root = Path(run_dir) / "trace" / "calls" if run_dir else None
    entries = sorted(root.iterdir()) if root and root.is_dir() else []
    if len(entries) > max_calls:
        entries = entries[: max_calls // 2] + [None] + entries[-max_calls // 2:]
    for entry in entries:
        if entry is None:
            lines.append("  ... calls omitted ...")
            continue
        name = entry.name.split("-", 2)[-1]
        provider = name.startswith(PROVIDERS)
        args = _read_json(entry / "input.json")
        output = _read_json(entry / "output.json")
        error = _read_json(entry / "error.json")
        ok = error is None and not (isinstance(output, dict) and output.get("ok") is False)
        indent = "      " if provider else "  "
        status = "ok" if ok else "FAILED"
        if isinstance(output, dict) and isinstance(output.get("error"), dict):
            status += f" {output['error'].get('code', '')}".rstrip()
        lines.append(f"{indent}{entry.name.split('-', 2)[1]} {name}: {status}")
        width = call_chars * (1 if ok else 3)
        if args is not None:
            lines.append(f"{indent}  in:  {_clip(json.dumps(compact(args)), width)}")
        if output is not None:
            lines.append(f"{indent}  out: {_clip(json.dumps(compact(output)), width)}")
        if error is not None:
            lines.append(f"{indent}  error: {_clip(json.dumps(compact(error)), width)}")
    if not entries:   # no call directory: the program's own CallTrace records
        for call in calls(run_dir):
            status = "ok" if call.get("ok") else f"FAILED {call.get('failure_code') or ''}".rstrip()
            lines.append(f"  [{call.get('index')}] {call.get('tool')} -> {call.get('result_type')} {status}")
            if call.get("error_message"):
                lines.append(f"      error: {_clip(call['error_message'], call_chars)}")
    result = program_result(run_dir)
    if result.get("result") is not None:
        lines.append(f"  result: {_clip(json.dumps(compact(result['result'])), 2000)}")
    if result.get("stdout"):
        lines.append(f"  stdout: {_clip(result['stdout'], 1500)}")
    return "\n".join(lines)


def keyframes(run_dir: Path | str | None, *, camera: str = "agentview", limit: int = 3) -> list[tuple[str, Path]]:
    """The keyframes that localize a failure: the one after the first failed motion call when there is one, the
    last motion's after-frame, and the reset frame; (label, png) pairs, most informative first."""
    if not run_dir:
        return []
    root = Path(run_dir) / "media" / "keyframes"
    if not root.is_dir():
        return []
    frames = sorted(p for p in root.iterdir() if (p / f"{camera}.png").is_file())
    if not frames:
        return []
    chosen: list[Path] = []
    trace = calls(run_dir)
    failed = [i for i, c in enumerate(trace) if not c.get("ok") and c.get("tool") in MOTION_TOOLS]
    if failed:
        # the failing call's own frames: the k-th call of its tool is the k-th frame pair of that tool (an exception
        # leaves no after-frame, so its before-frame stands in)
        tool = trace[failed[0]]["tool"]
        k = sum(1 for c in trace[:failed[0]] if c.get("tool") == tool)
        slug = str(tool).replace("_", "-")
        for when in ("after", "before"):
            mine = [p for p in frames if p.name.split("-", 1)[-1] == f"{when}-{slug}"]
            if len(mine) > k:
                chosen.append(mine[k])
                break
    chosen += [frames[-1], frames[0]]
    seen, picked = set(), []
    for frame in chosen:
        if frame not in seen:
            seen.add(frame)
            picked.append((frame.name, frame / f"{camera}.png"))
    return picked[:limit]


def image_part(png: Path) -> dict:
    return {"type": "image_url",
            "image_url": {"url": "data:image/png;base64," + base64.b64encode(png.read_bytes()).decode("ascii")}}
