"""End-to-end check of the user channel on Hermit main, unchanged: a real Harbor trial (Docker), Codex installed by
Harbor, a scripted model on this host, the scripted user in utils/scripted_user.py.

    PYTHONPATH=<hermit checkout> <python with hermit's dependencies> smoke.py <work dir> [adapter dir]
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import threading

HERE = Path(__file__).resolve().parent
GATEWAY = os.environ.get(
    "DOCKER_GATEWAY", "10.100.0.1"
)  # the host, as a container reaches it
TOML = """schema_version = "1.4"

[metadata]
author_name = "hermit"
difficulty = "easy"
category = "smoke"
tags = ["smoke"]

[verifier]
timeout_sec = 60.0

[agent]
timeout_sec = 600.0

[environment]
build_timeout_sec = 300.0
cpus = 1
memory_mb = 1024
"""
TEST_SH = """#!/bin/bash
# The reward of a session with a simulated user is the user's rating, which the agent class left in the agent's logs.
mkdir -p /logs/verifier
if [ -s /logs/agent/user-reward.txt ]; then
  cp /logs/agent/user-reward.txt /logs/verifier/reward.txt
  echo "user verdict:"; cat /logs/agent/user-verdict.json
else
  echo "no user rating: this trial is not scored"
fi
"""
TURNS = [
    {"cmd": "echo one >> /app/a.txt"},
    {"cmd": "sleep 4; echo two >> /app/a.txt"},
    "REPLY-1",
    {"cmd": "sleep 4; echo three >> /app/a.txt"},
    {"cmd": "echo four >> /app/a.txt"},
    "REPLY-2",
    "REPLY-3",
    "REPLY-EXTRA",
]


def js(command):
    return (
        'try {\n  const r = await tools.exec_command({"cmd": '
        + json.dumps(command)
        + "});\n  text(r);\n"
        '} catch (e) {\n  text("CAUGHT: " + String(e));\n}'
    )


def items_of(turn, n):
    if isinstance(turn, str):
        return [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": turn}],
            }
        ]
    return [
        {
            "type": "custom_tool_call",
            "name": "exec",
            "call_id": f"call{n}",
            "input": js(turn["cmd"]),
        }
    ]


def serve():
    requests, lock = [], threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(404)
            self.send_header("content-length", "0")
            self.end_headers()

        def do_POST(self):
            body = json.loads(
                self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}"
            )
            with lock:
                n = len(requests)
                requests.append(body)
            items = [
                dict(item, id=f"it{n}_{k}", status="completed")
                for k, item in enumerate(items_of(TURNS[min(n, len(TURNS) - 1)], n))
            ]
            events = [
                {
                    "type": "response.created",
                    "response": {"id": f"r{n}", "status": "in_progress", "output": []},
                }
            ]
            events += [
                {"type": "response.output_item.done", "output_index": k, "item": item}
                for k, item in enumerate(items)
            ]
            events.append(
                {
                    "type": "response.completed",
                    "response": {
                        "id": f"r{n}",
                        "status": "completed",
                        "output": items,
                        "usage": {
                            "input_tokens": 1,
                            "input_tokens_details": {"cached_tokens": 0},
                            "output_tokens": 1,
                            "output_tokens_details": {"reasoning_tokens": 0},
                            "total_tokens": 2,
                        },
                    },
                }
            )
            data = b"".join(
                f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode()
                for e in events
            )
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://{GATEWAY}:{server.server_address[1]}/v1", requests


def main():
    root = Path(sys.argv[1]).resolve()
    adapter = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else None
    task = root / "tasks" / "user-demo"
    for rel, text in {
        "task.toml": TOML,
        "instruction.md": "Append the lines one and two to /app/a.txt.\n",
        "environment/Dockerfile": "FROM "
        + os.environ.get("SMOKE_IMAGE", "python:3.11-slim")
        + "\nWORKDIR /app\n",
        "tests/test.sh": TEST_SH,
    }.items():
        (task / rel).parent.mkdir(parents=True, exist_ok=True)
        (task / rel).write_text(text)
    (task / "tests" / "test.sh").chmod(0o755)
    from hermit.harbor import dataset
    from hermit.harbor.judge import score
    from hermit.harbor.runner import HarborAgent

    rows = {r["id"]: r for r in dataset.index(root / "tasks")}
    if adapter is not None:  # the adapter's Stop hook sends the first answer back once
        TURNS.insert(2, "DRAFT-1")
    server, url, requests = serve()
    os.environ["MOCK_API_KEY"] = "dummy"
    agent = HarborAgent(
        {
            "harbor": os.environ.get(
                "HARBOR_BIN", str(Path.home() / ".cache/hermit/harbor-venv/bin/harbor")
            ),
            "tasks_dir": str(root / "tasks"),
            "jobs_dir": str(root / "jobs"),
            "disk_gate_gb": 5,
            "model": {
                "model": "gpt-5.6-luna",
                "base_url": url,
                "api_key_env": "MOCK_API_KEY",
            },
            "codex_version": "0.164.0",
            "codex_login": False,
            "agent_timeout_sec": int(os.environ.get("SMOKE_TIMEOUT", "600")),
            "sandbox_enforce": "best_effort",
            "environment": {"delete": True, "kwargs": {"auto_mounting": True}},
            "acting_agent": {
                "import_path": "hermit_user_codex:HermitCodex",
                "pythonpath": [str(HERE.parent / "utils"), str(HERE)],
                "kwargs": {
                    "user": {
                        "simulate": os.environ.get(
                            "SMOKE_SIM", "scripted_user:simulate"
                        ),
                        "params": {"persona": "demo"},
                        "wait_s": 120,
                    }
                },
            },
        },
        base=root,
    )
    out = agent.run(rows["user-demo"], adapter)
    server.shutdown()
    blobs = [json.dumps(r) for r in requests]
    first = {
        t: next((i for i, b in enumerate(blobs) if t in b), None)
        for t in (
            "FIRST:",
            "USER-INSERT",
            "USER-FOLLOWUP",
            "USER-INTERRUPT",
            "USER-FINAL",
            "ADAPTER-CHECK",
        )
    }
    log = Path(out.log) if out.log else None
    report = {
        "output": out.output,
        "judge": score(rows["user-demo"], out),
        "model_requests": len(requests),
        "first_request_with": first,
        "agent_metadata_user": (out.extra.get("agent_metadata") or {}).get("user"),
        "replies_heard": [
            r.get("text")
            for r in json.loads(
                (Path(out.log) / "agent" / "user-verdict.json").read_text()
            )
            .get("record", {})
            .get("replies", [])
        ]
        if out.log and (Path(out.log) / "agent" / "user-verdict.json").is_file()
        else None,
        "deployed": out.extra.get("deployed"),
        "exception": [
            out.extra.get("exception_type"),
            out.extra.get("exception_message"),
        ],
        "log_agent_files": sorted(p.name for p in (log / "agent").iterdir())
        if log
        else None,
        "verifier_stdout": (log / "verifier" / "test-stdout.txt").read_text()[:600]
        if log and (log / "verifier" / "test-stdout.txt").is_file()
        else None,
        "log": str(log),
    }
    (root / "report.json").write_text(json.dumps(report, indent=1, default=str))
    print(json.dumps(report, indent=1, default=str))
    assert out.output["status"] == "ok", report["exception"]
    assert all(
        first[name] is not None
        for name in (
            "FIRST:",
            "USER-INSERT",
            "USER-FOLLOWUP",
            "USER-INTERRUPT",
            "USER-FINAL",
        )
    )
    assert report["agent_metadata_user"]["interrupts"] == 1
    assert (
        "four"
        not in json.loads((log / "agent/user-verdict.json").read_text())["record"][
            "workspace"
        ]
    )


if __name__ == "__main__":
    main()
