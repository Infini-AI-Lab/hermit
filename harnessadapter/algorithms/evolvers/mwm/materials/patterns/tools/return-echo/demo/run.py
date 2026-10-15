import os as _os
_SCRATCH=_os.path.join(_os.path.dirname(_os.path.abspath(__file__)),'scratch')
_os.makedirs(_SCRATCH,exist_ok=True)
#!/usr/bin/env python3
"""return-echo demo: every tool return ends with a worksheet echo.

Two toy nursery tools share one helper: whatever a tool has to say,
its return ends with a compact echo of the current worksheet (open
batch ids, counts, last action), rendered at return time so the anchor
re-enters context at each decision moment. An unreadable worksheet
makes a tool refuse rather than echo stale memory.

Usage: python3 run.py
"""

import json
import shutil
import sys

ECHO_TOP = "---- worksheet ----"
ECHO_END = "-------------------"


def render_echo(ws):
    open_ids = [b["id"] for b in ws["batches"] if b["status"] == "open"]
    benched = sum(1 for b in ws["batches"] if b["status"] == "benched")
    return "\n".join([
        ECHO_TOP,
        f"open: {len(open_ids)} {open_ids}",
        f"benched: {benched}",
        f"last: {ws.get('last', '-')}",
        ECHO_END,
    ])


def with_echo(body, ws):
    """Every tool return funnels through here — no exceptions."""
    return body + "\n" + render_echo(ws)


def _load(path):
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return None


def _save(path, ws):
    json.dump(ws, open(path, "w"))


def tool_log_sowing(path, batch_id, desc):
    ws = _load(path)
    if ws is None:
        return "CATCH: worksheet unreadable; refusing to answer from memory"
    ws["batches"].append({"id": batch_id, "desc": desc, "status": "open"})
    ws["last"] = f"log_sowing({batch_id})"
    _save(path, ws)
    return with_echo(f"logged {batch_id}: {desc}", ws)


def tool_mark_benched(path, batch_id):
    ws = _load(path)
    if ws is None:
        return "CATCH: worksheet unreadable; refusing to answer from memory"
    hits = [b for b in ws["batches"] if b["id"] == batch_id]
    if not hits:
        return with_echo(f"no such batch {batch_id}; nothing changed", ws)
    hits[0]["status"] = "benched"
    ws["last"] = f"mark_benched({batch_id})"
    _save(path, ws)
    return with_echo(f"{batch_id} marked benched", ws)


def main():
    work = _os.path.join(_SCRATCH,'work.json')
    shutil.copy("sample_data/worksheet_start.json", work)
    print(tool_log_sowing(work, "TB-9", "2 trays chives"), "\n")
    print(tool_mark_benched(work, "TB-7"), "\n")
    return work


if __name__ == "__main__":
    main()
    sys.exit(0)
