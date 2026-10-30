#!/usr/bin/env python3
"""A chat-completions endpoint whose every reply is one Codex session, for learners that call the run's `llm` tool
(GEPA, SkillOpt, Continual Harness) when their model is reachable only through Codex: a ChatGPT login (a Codex plan,
codex_login.py). The learner then is Codex with that model, as the acting agent is.

    python codex_chat.py --port 3671 [--concurrency 16] [--timeout 3000] [--log-dir DIR]

and in the run config

    backends:
      codexrelay: {base_url: "http://127.0.0.1:3671/v1", key_required: false, api_key_env: null, apis: [chat]}
    llm: {model: codexrelay/gpt-5.6-luna, extra_body: {reasoning_effort: medium}, timeout: 3600}

POST /v1/chat/completions {model, messages, reasoning_effort?, ...}: one `codex exec` in a Docker container of its own
(codex_session.py's image, internal network and egress proxy; Codex's read-only sandbox inside) with an empty
workspace and a fresh CODEX_HOME (codex_login.py's settings and a copy of the login, removed when the session ends).
The system messages are the session's developer instructions; the user message is the prompt (several turns are
written out as a transcript), given on stdin. The reply is the session's final message, with the session's token
usage. Sampling settings Codex has no setting for (max_tokens, temperature) are recorded, not applied. A session waits
while the runs are paused, and one refused for the plan's limit is run again. GET /health answers ok.
Each call's request, settings, events and reply stay under --log-dir.

    OFFICEQA_CODEX_IMAGE / OFFICEQA_CODEX_NETWORK / OFFICEQA_CODEX_PROXY    as for codex_session.py
"""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import itertools
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import threading
import time
import tomllib

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import codex_login  # noqa: E402
from codex_session import IMAGE, NETWORK, PROXY, SECURITY, mount  # noqa: E402

EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")
UNAVAILABLE = ("unexpected status 429", "unexpected status 5", "exceeded retry limit", "stream disconnected",
               "error sending request", "too many requests", "service unavailable", "bad gateway",
               "error response from daemon", "cannot connect to the docker daemon")


def text_of(content) -> str:
    """A message's text: a string, or the text parts of a list of parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(p.get("text") or "") for p in content if isinstance(p, dict) and p.get("type") in
                         ("text", "input_text", "output_text"))
    return "" if content is None else str(content)


def split(messages: list[dict]) -> tuple[str, str]:
    """(developer instructions, prompt): the system (and developer) messages, and the rest - the one user message as
    it is, or several turns written out as a transcript that ends with the turn to answer."""
    system = "\n\n".join(text_of(m.get("content")) for m in messages if m.get("role") in ("system", "developer"))
    rest = [m for m in messages if m.get("role") not in ("system", "developer")]
    if len(rest) == 1 and rest[0].get("role") == "user":
        return system, text_of(rest[0].get("content"))
    turns = [f"[{m.get('role')}]\n{text_of(m.get('content'))}" for m in rest]
    return system, ("The conversation so far, oldest first; answer its last user turn as the assistant.\n\n"
                    + "\n\n".join(turns))


class Relay:
    def __init__(self, log_dir: Path, concurrency: int, timeout: float):
        self.log_dir = Path(log_dir).resolve()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.slots = threading.BoundedSemaphore(concurrency)
        self.timeout = timeout
        self.count = itertools.count(1)
        self.docker = shutil.which("docker") or "docker"

    def session(self, body: dict) -> tuple[int, dict]:
        """One request: (HTTP status, response body)."""
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            return 400, {"error": {"message": "messages: a non-empty list"}}
        model = str(body.get("model") or "")
        effort = str(body.get("reasoning_effort") or "medium")
        if effort not in EFFORTS:
            return 400, {"error": {"message": f"reasoning_effort {effort!r} is not one of {EFFORTS}"}}
        system, prompt = split(messages)
        call = f"{time.strftime('%Y%m%dT%H%M%S')}-{next(self.count):05d}-{secrets.token_hex(2)}"
        root = self.log_dir / call
        attempt = 0
        with self.slots:
            while True:
                codex_login.wait_for_plan(log=lambda m: print(f"codex_chat {call}: {m}", flush=True))
                shutil.rmtree(root, ignore_errors=True)
                work, home = root / "work", root / "home"
                work.mkdir(parents=True)
                home.mkdir(parents=True)
                (root / "request.json").write_text(json.dumps(body, ensure_ascii=False, indent=1))
                (root / "prompt.txt").write_text(prompt)
                lines = [*codex_login.config_lines(), "features.shell_snapshot = false", "features.multi_agent = false"]
                if system:
                    lines.append(f"developer_instructions = {json.dumps(system, ensure_ascii=False)}")
                lines += ["", "[shell_environment_policy]", 'inherit = "core"',
                          'exclude = ["*KEY*", "*TOKEN*", "*SECRET*"]']
                text = "\n".join(lines) + "\n"
                tomllib.loads(text)
                (home / "config.toml").write_text(text)
                codex_login.install_auth(home)
                code, timed_out, wall = self._run(call, work, home, model, effort, root)
                codex_login.note_usage(home)
                codex_login.remove_auth(home)
                events = []
                raw = (root / "events.jsonl").read_text(errors="replace") if (root / "events.jsonl").is_file() else ""
                for line in raw.splitlines():
                    if line.startswith("{"):
                        try:
                            events.append(json.loads(line))
                        except ValueError:
                            pass
                served = [e for e in events if e.get("type") == "turn.completed"]
                refusals = " ".join(str(e.get("message") or (e.get("error") or {}).get("message") or "")
                                    for e in events if e.get("type") in ("error", "turn.failed"))
                if not served and codex_login.limit_hit(refusals):
                    codex_login.hold_for_limit(log=lambda m: print(f"codex_chat {call}: {m}", flush=True))
                    continue
                if served or timed_out or not any(u in raw.lower() for u in UNAVAILABLE) or attempt == 2:
                    break
                attempt += 1
                time.sleep(60)
        last = work / ".last_message.txt"
        reply = last.read_text(errors="replace") if last.is_file() else ""
        if not reply.strip():
            texts = [(e.get("item") or {}).get("text") or "" for e in events if e.get("type") == "item.completed"
                     and (e.get("item") or {}).get("type") == "agent_message"]
            reply = texts[-1] if texts else ""
        (root / "reply.txt").write_text(reply)
        if not served or not reply.strip():
            why = ("timed out" if timed_out else f"exit {code}") + f": {(refusals or raw[-800:]).strip()[:800]}"
            print(f"codex_chat {call}: no reply ({why})", flush=True)
            return 503, {"error": {"message": f"the Codex session gave no reply ({why})", "type": "codex_session"}}
        usage = served[-1].get("usage") or {}
        prompt_tokens = int(usage.get("input_tokens") or 0)
        cached = int(usage.get("cached_input_tokens") or 0)
        completion = int(usage.get("output_tokens") or 0)
        reasoning = int(usage.get("reasoning_output_tokens") or 0)
        print(f"codex_chat {call}: {wall:.0f}s in={prompt_tokens} cached={cached} out={completion} "
              f"chars={len(reply)}", flush=True)
        shutil.rmtree(work, ignore_errors=True)             # empty but for what the session wrote
        return 200, {"id": f"chatcmpl-{call}", "object": "chat.completion", "created": int(time.time()), "model": model,
                     "choices": [{"index": 0, "message": {"role": "assistant", "content": reply},
                                  "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion,
                               "total_tokens": prompt_tokens + completion,
                               "prompt_tokens_details": {"cached_tokens": cached},
                               "completion_tokens_details": {"reasoning_tokens": reasoning}},
                     "codex": {"call": call, "wall_s": round(wall, 1)}}

    def _run(self, call: str, work: Path, home: Path, model: str, effort: str, root: Path) -> tuple[int, bool, float]:
        name = f"oqchat-{call.lower()}"
        args = ["codex", "exec", "--json", "--skip-git-repo-check", "-s", "read-only", "-C", str(work),
                *(["-m", model] if model else []), "-c", f'model_reasoning_effort="{effort}"',
                "-c", 'approval_policy="never"', "-o", str(work / ".last_message.txt"), "-"]
        cmd = [self.docker, "run", "--rm", "-i", "--name", name, "--label", "officeqa-codex=1", "--network", NETWORK,
               "--read-only", "--tmpfs", "/tmp:size=1g", "--pids-limit", "512", "--memory", "4g", *SECURITY,
               "--user", f"{os.getuid()}:{os.getgid()}", *mount(work), *mount(home), "-w", str(work),
               "-e", f"HOME={home}", "-e", f"CODEX_HOME={home}", "-e", f"HTTPS_PROXY={PROXY}", IMAGE, *args]
        (root / "command.json").write_text(json.dumps(cmd[cmd.index(IMAGE):], indent=1))
        started = time.time()
        with open(root / "prompt.txt", "rb") as stdin, open(root / "events.jsonl", "wb") as out:
            try:
                proc = subprocess.run(cmd, stdin=stdin, stdout=out, stderr=subprocess.STDOUT, timeout=self.timeout)
                return proc.returncode, False, time.time() - started
            except subprocess.TimeoutExpired:
                subprocess.run([self.docker, "rm", "-f", name], stdin=subprocess.DEVNULL, capture_output=True)
                return -1, True, time.time() - started


def serve(relay: Relay, port: int) -> None:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code: int, payload: dict) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path.rstrip("/").endswith("/health"):
                self._send(200, {"ok": True, "image": IMAGE, "proxy": PROXY})
            else:
                self._send(404, {"error": {"message": "not found"}})

        def do_POST(self):
            if not self.path.rstrip("/").endswith("/chat/completions"):
                self._send(404, {"error": {"message": "only /v1/chat/completions"}})
                return
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            except ValueError:
                self._send(400, {"error": {"message": "the body is not JSON"}})
                return
            try:
                code, payload = relay.session(body)
            except Exception as exc:                        # a failure of the relay itself: the client retries
                print(f"codex_chat: {type(exc).__name__}: {exc}", flush=True)
                code, payload = 500, {"error": {"message": f"{type(exc).__name__}: {exc}"}}
            self._send(code, payload)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    print(f"codex_chat on http://127.0.0.1:{port}/v1 (image {IMAGE}, proxy {PROXY})", flush=True)
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--timeout", type=float, default=3000, help="seconds per Codex session")
    parser.add_argument("--log-dir", default=os.path.expanduser("~/albz/officeqa-luna/relay"))
    args = parser.parse_args()
    serve(Relay(Path(args.log_dir), args.concurrency, args.timeout), args.port)


if __name__ == "__main__":
    main()
