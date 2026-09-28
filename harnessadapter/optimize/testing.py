"""$0 fixtures for optimizer Evolvers: a deterministic environment and a local
OpenAI-compatible model server. With them the real GEPA engine and the real
SkillOpt trainer (and its OpenAI SDK client) run end to end without a model.

KeywordEnvironment: every task has a kind; a task passes when the adapter's
context names the rule for its kind (RULE_<KIND>). Train feedback names the
missing rule, val feedback does not, which is the split policy real
environments follow.

FakeChatServer + scripted_optimizer: answer each optimizer prompt the way the
optimizer asks (GEPA's fenced instruction block, SkillOpt's JSON stages) by
adding the rules that train feedback said were missing.

    harnessadapter optimize prompt-only --evolver gepa \\
      --environment harnessadapter.optimize.testing:KeywordEnvironment --fake-optimizer
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import threading

from harnessadapter.adapter.loader import Adapter
from harnessadapter.optimize.environment import Environment, Rollout, Task

KINDS = ("ALPHA", "BETA", "GAMMA", "DELTA")


class KeywordEnvironment(Environment):
    name = "keyword-toy"

    def __init__(self, train: int = 8, val: int = 4, test: int = 4):
        self._tasks = {}
        self._split_of = {}
        counts = {"train": train, "val": val, "test": test}
        n = 0
        for split, count in counts.items():
            self._tasks[split] = []
            for i in range(count):
                kind = KINDS[n % len(KINDS)]
                self._tasks[split].append(Task(id=f"{split}-{i:02d}", split=split, kind=kind.lower(),
                                               prompt=f"Solve toy task {split}-{i:02d} of kind {kind}."))
                self._split_of[f"{split}-{i:02d}"] = split
                n += 1
        self.calls: list[dict] = []

    def tasks(self, split):
        return list(self._tasks[split])

    def describe(self):
        return {"name": self.name, "sizes": {k: len(v) for k, v in self._tasks.items()}}

    def evaluate(self, adapter, tasks, *, label):
        context = Adapter(Path(adapter)).context() if adapter else ""
        self.calls.append({"label": label, "tasks": [t.id for t in tasks]})
        rollouts = []
        for task in tasks:
            rule = f"RULE_{task.kind.upper()}"
            ok = rule in context
            reveal = self._split_of[task.id] == "train"          # the registered split, not the caller's label
            feedback = "CORRECT." if ok else ("INCORRECT. The adapter context requires " + rule + "."
                                              if reveal else "INCORRECT.")
            rollouts.append(Rollout(task_id=task.id, split=task.split, score=1.0 if ok else 0.0,
                                    output="done" if ok else "unsure", feedback=feedback,
                                    trace=f"[action] read context\n[obs] {len(context)} characters",
                                    steps=[{"type": "tool_call", "cmd": "cat AGENTS.md", "obs": f"{len(context)} characters"}]))
        return rollouts


def _rules(text: str) -> list[str]:
    found = set(re.findall(r"requires (RULE_[A-Z]+)", text))
    found |= set(re.findall(r'"content":\s*"- (RULE_[A-Z]+)', text))
    return sorted(found)


def scripted_optimizer(text: str) -> str:
    """One deterministic answer per optimizer stage."""
    rules = _rules(text)
    if "I provided an assistant with the following instructions" in text:          # GEPA reflection
        current = text.split("```", 2)[1] if text.count("```") >= 2 else ""
        current = current.split("\n", 1)[1] if current.startswith(("markdown", "md")) else current
        added = "".join(f"\n- {r}" for r in rules if r not in current)
        return "```\n" + current.strip() + added + "\n```"
    if "selected_indices" in text:                                                 # SkillOpt ranking
        return json.dumps({"reasoning": "all", "selected_indices": list(range(8))})
    if "slow_update_content" in text:
        return json.dumps({"reasoning": "keep", "slow_update_content": "Keep the rules that helped."})
    if "meta_skill_content" in text:
        return json.dumps({"reasoning": "keep", "meta_skill_content": "Prefer rules named by feedback."})
    edits = [{"op": "append", "content": f"- {r}", "support_count": 1, "source_type": "failure"} for r in rules]
    if "failure_summary" in text:                                                  # SkillOpt error analyst
        return json.dumps({"batch_size": 1, "failure_summary": [], "patch": {"reasoning": "rules", "edits": edits}})
    if "success_patterns" in text:
        return json.dumps({"batch_size": 1, "success_patterns": [], "patch": {"reasoning": "none", "edits": []}})
    return json.dumps({"reasoning": "merged", "edits": edits})                    # SkillOpt merges


class FakeChatServer:
    """A localhost /v1/chat/completions that answers with respond(prompt)."""

    def __init__(self, respond=scripted_optimizer):
        self.respond = respond
        self.requests: list[dict] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                server.requests.append(body)
                prompt = "\n\n".join(str(m.get("content", "")) for m in body.get("messages", []))
                text = server.respond(prompt)
                reply = json.dumps({"id": "fake", "object": "chat.completion", "created": 0, "model": body.get("model"),
                                    "choices": [{"index": 0, "finish_reason": "stop",
                                                 "message": {"role": "assistant", "content": text}}],
                                    "usage": {"prompt_tokens": len(prompt) // 4, "completion_tokens": len(text) // 4,
                                              "total_tokens": (len(prompt) + len(text)) // 4}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
