#!/usr/bin/env python3
"""Inside a sandbox: run one task's agent, or one call of a declared tool, and hand the result back.

    python worker.py          # the job arrives as JSON on stdin; HA_RESULT_FD names the pipe the result leaves by

A job is {"kind": "agent", "name", "params", "base", "task", "adapter"} (what `load_agent(...).run(task, adapter)`
takes) or {"kind": "tool", "entrypoint", "base", "arguments", "bound"} (a template's or task's declared tool). The
result is {"ok": true, "output": <RunOutput record>} / {"ok": true, "result": ...} or {"ok": false, "error",
"traceback"}.

The result pipe is made non-inheritable before anything else runs, so a program the agent starts cannot write a
result of its own (a score it did not earn, a cost it did not spend). Whatever the agent prints goes to its own
stdout and stderr; only this process writes to the pipe.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import traceback


def _run(job: dict) -> dict:
    if job["kind"] == "agent":
        from harnessadapter.adapter.session import ENV_PYTHON, ENV_SANDBOX
        from harnessadapter.agents.base import load_agent
        from harnessadapter.task import RunOutput, output_record
        # The adapter's tools never run in this process, nor in any it starts (inherited through the environment).
        os.environ[ENV_SANDBOX] = "1"
        os.environ[ENV_PYTHON] = sys.executable
        agent = load_agent(job["name"], job.get("params"), Path(job["base"]))
        out = agent.run(job["task"], Path(job["adapter"]) if job.get("adapter") else None)
        if not isinstance(out, RunOutput):
            raise TypeError(f"the pipeline returned {type(out).__name__}, not RunOutput")
        return {"ok": True, "output": output_record(out)}
    if job["kind"] == "tool":
        import inspect
        from harnessadapter.tools import _load_entry
        fn = _load_entry(job["entrypoint"], Path(job["base"]))
        wanted = inspect.signature(fn).parameters
        bound = {k: Path(v) for k, v in (job.get("bound") or {}).items() if k in wanted}
        return {"ok": True, "result": fn(**(job.get("arguments") or {}), **bound)}
    raise ValueError(f"unknown job kind: {job.get('kind')!r}")


def _not_dumpable() -> None:
    """Make /proc/<this process> unreadable to the programs the agent starts: through /proc/<pid>/fd they could
    otherwise open the result pipe this process holds."""
    try:
        import ctypes
        ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0)      # PR_SET_DUMPABLE, 0
    except Exception:
        pass


def main() -> None:
    fd = int(os.environ.pop("HA_RESULT_FD"))
    os.set_inheritable(fd, False)
    _not_dumpable()
    with os.fdopen(fd, "w") as result:
        try:
            payload = _run(json.loads(sys.stdin.read()))
        except BaseException as exc:            # the host turns this into the task's error record
            payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                       "traceback": traceback.format_exc()[-3000:]}
        result.write(json.dumps(payload, default=str))


if __name__ == "__main__":
    main()
