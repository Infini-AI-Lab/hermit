#!/usr/bin/env python3
"""Read-only SQL endpoint for the learner (the framework's `sql` binding).

    POST /sql      {"q": "<one SELECT/WITH/PRAGMA/EXPLAIN statement>"}
                   -> {"columns": [...], "rows": [...], "truncated": bool, "row_cap": N} | {"error": "..."}
    GET  /health   -> {"ok": true, "db": "<current db path or null>"}

The database it serves is whatever path the host writes into --db-pointer
(the venue registers the current instance's database there, atomically), so
a statement between blocks runs against the instance the hosts just played.
Guarantees ported from metaworld mwm/toolsrv.py (22b67ea): driver read-only
(mode=ro), PRAGMA query_only, a statement timeout, verb screening, a row cap,
an output-character cap, and an append-only statement log (--log).
"""
import argparse
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_SQL_MAX_ROWS = 50
_SQL_TIMEOUT_S = 10
_SQL_OUT_CHARS = 4000
_LEADING_COMMENT = re.compile(r"^\s*(?:--[^\n]*(?:\n|$)|/\*.*?\*/\s*)+", re.S)


def strip_leading_comments(q: str) -> str:
    return _LEADING_COMMENT.sub("", q or "", count=1).strip()


def screen_sql(q: str) -> str:
    """The verb and PRAGMA screening of the read channel; "" when the
    statement may run, else the refusal text."""
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


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--db-pointer", required=True, help="File whose contents are the current database path")
    p.add_argument("--log", required=True, help="Append-only JSONL of every statement")
    args = p.parse_args()

    def current_db():
        try:
            path = open(args.db_pointer).read().strip()
        except OSError:
            return None
        return path if path and os.path.exists(path) else None

    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _reply(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.rstrip("/") == "/health":
                return self._reply(200, {"ok": True, "db": current_db()})
            self._reply(404, {"error": "unknown path"})

        def do_POST(self):
            if self.path.rstrip("/") != "/sql":
                return self._reply(404, {"error": "unknown path"})
            n = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._reply(400, {"error": "body must be JSON"})
            q = str(payload.get("q") or "")
            t0 = time.time()
            refusal = screen_sql(q)
            db = current_db()
            if refusal:
                out = {"error": refusal}
            elif db is None:
                out = {"error": "no database is registered for this run at the moment"}
            else:
                out = sql_readonly(db, strip_leading_comments(q))
            with lock, open(args.log, "a") as f:
                f.write(json.dumps({"t": round(t0, 3), "ms": round((time.time() - t0) * 1000),
                                    "q": q[:2000], "rows": len(out.get("rows", [])) if "rows" in out else None,
                                    "truncated": out.get("truncated"), "error": out.get("error")}) + "\n")
            self._reply(200, out)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"env_server: sql on http://{args.host}:{args.port} pointer={args.db_pointer}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
