"""The learner's read-only SQL channel at the database venue (a task tool, `evolver_access.tools`).

It runs against the database the hosts of the latest block played: the pipeline writes that path into
$MWM_TASK_WORKDIR/current_db after every question. Guarantees ported unchanged from the old port's env_server.py
(itself from metaworld mwm/toolsrv.py, 22b67ea): driver read-only (mode=ro), PRAGMA query_only, verb screening,
a 10 s statement timeout, a 50-row cap, a 4,000-character output cap, and an append-only statement log.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import threading
import time

_SQL_MAX_ROWS = 50
_SQL_TIMEOUT_S = 10
_SQL_OUT_CHARS = 4000
_LEADING_COMMENT = re.compile(r"^\s*(?:--[^\n]*(?:\n|$)|/\*.*?\*/\s*)+", re.S)
_LOCK = threading.Lock()


def strip_leading_comments(q: str) -> str:
    return _LEADING_COMMENT.sub("", q or "", count=1).strip()


def screen_sql(q: str) -> str:
    """The verb and PRAGMA screening of the read channel; "" when the statement may run, else the refusal text."""
    q = strip_leading_comments(q)
    if not q:
        return "query required: sql \"<one SELECT statement>\""
    head = q.lstrip("(").split(None, 1)[0].lower() if q else ""
    if head not in ("select", "with", "pragma", "explain"):
        return (f"refused: only SELECT/WITH/PRAGMA/EXPLAIN are allowed here, "
                f"not {head.upper()!r}. This is a read channel.")
    if re.search(r"\b(pragma_)?(database_list|temp_store_directory|"
                 r"data_store_directory)\b", q, re.I):
        return ("refused: that exposes the host file layout; the database "
                "path is deliberately not available through this channel.")
    return ""


def sql_readonly(db: str, q: str) -> dict:
    import sqlite3
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        try:
            conn.execute("PRAGMA query_only=ON")
            timer = threading.Timer(_SQL_TIMEOUT_S, conn.interrupt)
            timer.start()
            try:
                cur = conn.execute(q)
                cols = [d[0] for d in (cur.description or [])]
                rows = cur.fetchmany(_SQL_MAX_ROWS + 1)
            finally:
                timer.cancel()
        finally:
            conn.close()
    except sqlite3.Error as e:
        return {"error": f"sqlite: {e}"}
    truncated = len(rows) > _SQL_MAX_ROWS
    rows = [[v if v is None or isinstance(v, (int, float)) else str(v)[:200] for v in r]
            for r in rows[:_SQL_MAX_ROWS]]
    out = {"columns": cols, "rows": rows, "truncated": truncated, "row_cap": _SQL_MAX_ROWS}
    while rows and len(json.dumps(out)) > _SQL_OUT_CHARS:
        rows.pop()
        out = {"columns": cols, "rows": rows, "truncated": True, "row_cap": _SQL_MAX_ROWS}
    return out


def _workdir() -> Path:
    value = os.environ.get("MWM_TASK_WORKDIR")
    if not value:
        raise RuntimeError("MWM_TASK_WORKDIR is not set (the launcher sets it to the run's task directory)")
    return Path(value)


def current_db() -> str | None:
    try:
        path = (_workdir() / "current_db").read_text().strip()
    except OSError:
        return None
    return path if path and os.path.exists(path) else None


def sql(query: str) -> dict:
    t0 = time.time()
    refusal = screen_sql(query)
    db = current_db()
    if refusal:
        out = {"error": refusal}
    elif db is None:
        out = {"error": "no database is registered for this run at the moment"}
    else:
        out = sql_readonly(db, strip_leading_comments(query))
    with _LOCK, open(_workdir() / "meta_sql.jsonl", "a") as f:
        f.write(json.dumps({"t": round(t0, 3), "ms": round((time.time() - t0) * 1000), "q": str(query)[:2000],
                            "rows": len(out.get("rows", [])) if "rows" in out else None,
                            "truncated": out.get("truncated"), "error": out.get("error")}) + "\n")
    return out
