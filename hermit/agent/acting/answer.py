"""The `answer` agent: an acting agent that only answers, one model call, its answer written where the task's verifier
reads it. For a task whose work is a reply (a question about a figure, a value from a document) rather than a
session of work in the task's environment.

    agent:
      name: answer
      params:
        model: fireworks/accounts/fireworks/models/glm-5p3   # a <backend>/<model> reference, as react's
        instruction_file: /app/instruction.md   # optional: the question is this file of the task's environment (a
                                                # task whose entrypoint writes it), waited for up to wait_s; else the
                                                # task's instruction (its prompt-like field)
        attachments: ["/app/*.png"]             # optional: globs of images in the environment, sent with the question
        answer_file: /app/answer.txt            # optional: where the answer is written in the environment
        answer_pattern: '<answer>(.*?)</answer>'   # optional: the answer is the reply's last match (group 1)
        answer_format: "{answer}"               # optional: the text written, with {answer} in it
        max_tokens: 16384
        wait_s: 30

The adapter's context (ADAPTER.md, and what its context tool renders for the question) is the call's system message;
without an adapter there is none. The output is {answer, reply}; the log keeps the request's messages (an image as
its path, never its bytes) and the reply. Without the task's environment (a task set that gives none) the answer is
only returned. A figure over MAX_ATTACHMENT_BYTES is left out, as one that is no image.
"""
from __future__ import annotations

import base64
import json
import mimetypes
from pathlib import Path
import re
import tempfile
import time

from hermit.agent.acting.agent import Agent
from hermit.agent.base.react import total as usage_total
from hermit.agent.model.direct import Direct
from hermit.agent.model.llm import ChatClient
from hermit.common import RunOutput

INPUT_FIELDS = ("prompt", "question", "instruction", "input")
MAX_ATTACHMENT_BYTES = 20 * 2**20
DEFAULT_WAIT_S = 30.0
POLL_S = 1.0


class AnswerAgent(Agent):
    supports = ("context", "context_tool")       # the adapter's context is the call's system message

    def run(self, task: dict, adapter: Path | None, environment=None) -> RunOutput:
        p = self.params
        model = p.get("model")
        if not isinstance(model, dict) or not model.get("base_url") or not model.get("model"):
            raise ValueError("agent.params.model is required: a `<backend>/<model>` reference")
        started = time.time()
        question = self.question(task, environment)
        session = self.session(adapter)
        try:
            system = session.context(query=question, task=task) if session is not None else ""
            record = session.last_context if session is not None else None
        finally:
            if session is not None:
                session.close()
        images, named = self.attachments(environment)
        content = [{"type": "text", "text": question}, *({"type": "image_url", "image_url": {"url": url}}
                                                        for url in images)] if images else question
        messages = ([{"role": "system", "content": system}] if system.strip() else []) + \
            [{"role": "user", "content": content}]
        client = ChatClient.from_spec(model)
        reply = Direct({"max_tokens": int(p.get("max_tokens") or 16384)}, client).run(messages)
        answer = self.answer(reply.reply)
        if environment is not None and p.get("answer_file"):
            text = str(p.get("answer_format") or "{answer}").replace("{answer}", answer)
            with tempfile.TemporaryDirectory(prefix="hermit-answer-") as tmp:
                local = Path(tmp) / "answer.txt"
                local.write_text(text, encoding="utf-8")
                environment.upload(local, str(p["answer_file"]))
        logs = self.log_dir()
        log = Path(tempfile.mkstemp(prefix=f"{task.get('id', 'task')}-", suffix=".json", dir=logs)[1])
        shown = [{**m, "content": question if isinstance(m["content"], list) else m["content"]} for m in messages]
        log.write_text(json.dumps({"task": task.get("id"), "model": model["model"], "messages": shown,
                                   "attachments": named, "reply": reply.reply, "answer": answer},
                                  ensure_ascii=False, indent=1))
        usage = usage_total(client, reply.usage)
        extra = {"attachments": named}
        if record is not None:
            extra["context_tool"] = record
        return RunOutput(output={"answer": answer, "reply": reply.reply}, log=log, cost_usd=usage.cost_usd,
                         wall_s=round(time.time() - started, 1), tokens=usage.input_tokens + usage.output_tokens,
                         api_speed={"model_calls": usage.calls},
                         messages=[*shown, {"role": "assistant", "content": reply.reply}], extra=extra)

    def question(self, task: dict, environment) -> str:
        """The instruction file of the environment (waited for), else the task's prompt-like field."""
        path = self.params.get("instruction_file")
        if path and environment is not None:
            deadline = time.time() + float(self.params.get("wait_s") or DEFAULT_WAIT_S)
            while True:
                found = environment.exec(["cat", str(path)], timeout=60)
                if found.code == 0:
                    return found.output.strip()
                if time.time() >= deadline:
                    raise RuntimeError(f"the task's environment has no {path} (waited "
                                       f"{self.params.get('wait_s') or DEFAULT_WAIT_S} s)")
                time.sleep(POLL_S)
        for field in INPUT_FIELDS:
            if isinstance(task.get(field), str):
                return task[field]
        raise ValueError(f"task {task.get('id')!r} has none of {INPUT_FIELDS}")

    def attachments(self, environment) -> tuple[list[str], list[str]]:
        """(the images as data URLs, their paths) the attachment globs match in the environment."""
        globs = list(self.params.get("attachments") or [])
        if not globs or environment is None:
            return [], []
        listing = environment.exec(["sh", "-c", "for f in " + " ".join(globs) + "; do [ -f \"$f\" ] && echo \"$f\"; "
                                    "done; true"], timeout=60)
        if listing.code != 0:
            raise RuntimeError(f"the attachments could not be listed: {listing.output[-500:]}")
        urls, named = [], []
        for path in [line for line in listing.output.splitlines() if line.strip()]:
            kind = mimetypes.guess_type(path)[0] or ""
            if not kind.startswith("image/"):
                continue
            with tempfile.TemporaryDirectory(prefix="hermit-attachment-") as tmp:
                local = Path(tmp) / Path(path).name
                environment.download(path, local)
                if local.stat().st_size > MAX_ATTACHMENT_BYTES:
                    continue
                urls.append(f"data:{kind};base64,{base64.b64encode(local.read_bytes()).decode()}")
                named.append(path)
        return urls, named

    def answer(self, reply: str) -> str:
        pattern = self.params.get("answer_pattern")
        if not pattern:
            return reply.strip()
        found = list(re.finditer(str(pattern), reply, re.DOTALL))
        if not found:
            return reply.strip()
        last = found[-1]
        return (last.group(1) if last.groups() else last.group(0)).strip()
