"""Reactions (v20s): the launcher rules as they stood when r20 ran (fc_multi/ma_run.py, 2026-09-29 11:45), for the
replication of r20 on the service stack. Kept: host-side ingest, the notice gate as it was then (requests older
than 10 min dropped, urgent or not; no busy guard; no `defer`), resume of an ended session, snapshots, served-notice
log. Not present then, so absent here: the phase-1 push (09-30) and the idle rule (09-29 14:20).
Below: the v30 module with those rules removed or reverted (marked v20s). The standalone service (hermit/runtime/service.py) calls `react` every tick
(5 s) with the run script's status report; `react` returns commands for the run script: `interrupt` (stop the session,
resume it with the text) and `resume` (resume a session that ended, with the text). The run script decides nothing.

Rules (numbers of docs/async-collab-adapter-service-design-2026-09-30.md, section 5):
  1 host-side ingest of a changed Problem.lean (at most every 30 s per agent, one at a time)
  2 the notice gate over library/interrupts.jsonl
  3 phase-1 push (no map, 15 min of silence)
  4 idle rule (phase 2, 10 min on no task; a registered agent silent for 15 min counts as on none)
  5 snapshots of state.json every 30 s
  6 served-notice log (interrupts.done / interrupts.log)
  7 resume an ended session (not proved, more than 2 min left, at most 3 resumes)

Status report (per agent, from the run script): running (bool), t_start, t_interrupted, resumes, exited_at (or None),
busy (bool), grading (bool), proved (bool), restarted (bool: its session died with the machine); plus top-level t0 and cap.
"""
import concurrent.futures as cf
import contextvars
import json
import os
import time
from pathlib import Path

import collab
import assembly

try:
    from hermit.runtime.agent_context import AGENT_ENV
except ImportError:   # outside the service there is no per-call context
    AGENT_ENV = None

RESUMES = 3
_S = {"init": False, "handled": set(), "local": {}, "pool": None, "last_snap": 0.0}


def _mtime(p, default=0.0):
    try:
        return os.path.getmtime(p)
    except OSError:
        return default


def _init(lib: Path):
    if _S["init"]:
        return
    done = lib.parent / "interrupts.done"
    if done.exists():
        _S["handled"] |= {l.strip() for l in done.read_text().splitlines() if l.strip()}   # a restarted service does not re-deliver
    _S["pool"] = cf.ThreadPoolExecutor(5)
    _S["init"] = True


def _in_agent_context(env: dict, fn, *args, **kw):
    ctx = contextvars.copy_context()
    def run():
        if AGENT_ENV is not None:
            AGENT_ENV.set(env)
        return fn(*args, **kw)
    return ctx.run(run)


def _ingest(env: dict):
    lib = env["COLLAB_DIR"]
    try:
        _in_agent_context(env, collab.run_check, lib, env["COLLAB_AGENT"], env["COLLAB_WORKSPACE"], env["COLLAB_TARGET"],
                          env["COLLAB_REPO"], env["COLLAB_LAKE"], deliver=False)
    except Exception as exc:
        with open(os.path.join(os.path.dirname(lib), "collab_errors.log"), "a") as lg:
            lg.write(f'{time.strftime("%H:%M:%S")} ingest {env["COLLAB_AGENT"]}: {exc}\n')


def react(ev: dict) -> list:
    now = ev["now"]; status = ev["status"]; agents = ev["agents"]
    if not status or not agents:
        return []
    any_env = next(iter(agents.values()))
    lib = Path(any_env["COLLAB_DIR"]); base = lib.parent
    _init(lib)
    cap = float(status["cap"]); t0 = float(status["t0"]); st_agents = status["agents"]
    loc = _S["local"]
    for me in agents:
        loc.setdefault(me, {"t_interrupted": 0.0, "t_p1push": 0.0, "t_no_task": None, "ingested_mt": 0.0, "ingested_t": 0.0,
                            "ingest": None, "exit_seen": None})
    def a_of(me):
        a = dict(st_agents.get(me) or {})
        a["t_interrupted"] = max(float(a.get("t_interrupted") or 0), loc[me]["t_interrupted"])
        return a
    def work(me): return agents[me]["COLLAB_WORKSPACE"]
    out = []

    def deliver(me, rid, text, kind="interrupt"):
        """rule 6 and the launcher's stop+launch: the text is the notice, then the resume message (as ma_run did)"""
        try:
            msg = _in_agent_context(agents[me], collab.resume_text, str(lib), me)
        except Exception as exc:
            with open(base / "collab_errors.log", "a") as lg: lg.write(f'{time.strftime("%H:%M:%S")} resume_text {me}: {exc}\n')
            msg = "The target is not proved yet. Continue working on it."
        out.append(dict(kind=kind, agent=me, id=rid, text=(text + "\n\n" + msg) if text else msg))
        if kind == "interrupt":
            loc[me]["t_interrupted"] = now
            with open(base / "interrupts.log", "a") as lg: lg.write(f'{time.strftime("%H:%M:%S")} {me} interrupted: {text[:120]}\n')
            with open(base / "interrupts.done", "a") as dn: dn.write(rid + "\n")

    # rule 1: the library is filled from every changed file, whether or not the agent used `check`
    for me in agents:
        mt = _mtime(f"{work(me)}/Problem.lean"); L = loc[me]; ing = L["ingest"]
        if mt > L["ingested_mt"] and now - L["ingested_t"] >= 30 and (ing is None or ing.done()):
            L["ingested_mt"], L["ingested_t"] = mt, now
            L["ingest"] = _S["pool"].submit(_ingest, agents[me])

    # rule 2: the notice gate (ma_run.py, same order and numbers)
    ipath = lib / "interrupts.jsonl"
    if ipath.exists():
        for l in ipath.read_text().splitlines():
            try: req = json.loads(l)
            except Exception: continue
            if req["id"] in _S["handled"]: continue
            me = req["agent"]; a = a_of(me) if me in agents else None
            if now < req.get("not_before", 0): continue
            if now - req["t"] > 600: _S["handled"].add(req["id"]); continue   # v20s: 10 min for every request (the 25-min urgent window came 09-30 16:44)
            if not a or not a.get("running"): _S["handled"].add(req["id"]); continue
            left = cap - (now - float(a["t_start"]))
            if (now - a["t_interrupted"] < 300 and not req.get("urgent")) or left < 300: _S["handled"].add(req["id"]); continue
            last_call = _mtime(f"{work(me)}/tool-calls.jsonl")
            if now - last_call < 180 and not req.get("urgent"): continue
            _S["handled"].add(req["id"])
            deliver(me, req["id"], req["text"])

    # rules 3 and 4 (phase-1 push, idle rule) did not exist when r20 ran (v20s)

    # rule 8 (v12.1): automatic assembly — an agent's map whose parts are all proved is assembled in that agent's context
    try:
        if (lib / "state.json").exists():
            with collab._State(lib) as st8:
                due = [a8 for a8 in (st8.data.get("maps") or {}) if a8 in agents and assembly.ready(st8, a8)]
            busy8 = _S.setdefault("assembling", {})
            for a8 in due:
                if a8 in busy8 and not busy8[a8].done():
                    continue
                busy8[a8] = _S["pool"].submit(_in_agent_context, agents[a8], assembly.assemble_map, str(lib), a8)
    except Exception as exc:
        with open(base / "collab_errors.log", "a") as lg: lg.write(f'{time.strftime("%H:%M:%S")} assembly: {exc}\n')

    # rule 7: an agent that ended its session unproved with time left is resumed
    for me in agents:
        a = a_of(me); ex = a.get("exited_at")
        if ex is None or ex == loc[me]["exit_seen"] or a.get("grading") or a.get("proved"):
            continue
        loc[me]["exit_seen"] = ex
        if cap - (now - float(a["t_start"])) > 120 and (int(a.get("resumes") or 0) < RESUMES or a.get("restarted")):   # a session lost to a machine restart is resumed outside the budget (as ma_run did)
            deliver(me, f"resume:{me}:{int(now)}", "", kind="resume")

    # rule 5: snapshots of the shared state every 30 s
    if now - _S["last_snap"] >= 30 and (lib / "state.json").exists():
        _S["last_snap"] = now
        (base / "snapshots").mkdir(exist_ok=True)
        try:
            with collab._State(lib) as st:   # a consistent copy (taken under the state lock)
                data = json.dumps(st.data, ensure_ascii=False, indent=1)
            (base / "snapshots" / f"state_t{int(now - t0):05d}.json").write_text(data)
        except Exception as exc:
            with open(base / "collab_errors.log", "a") as lg: lg.write(f'{time.strftime("%H:%M:%S")} snapshot: {exc}\n')
    return out
