"""The `answer` agent: an acting agent that only answers, one model call, its answer written where the task's verifier
reads it. For a task whose work is a reply (a question about a figure, a value from a document) rather than a
session of work in the task's environment.

    agent:
      name: answer
      params:
        model: fireworks/accounts/fireworks/models/glm-5p3   # a <backend>/<model> reference, as react's
        instruction_file: /app/instruction.md   # optional: the question is this file of the task's environment (a
                                                # task whose entrypoint writes it), waited for up to wait_s; else the
                                                # environment's instruction (Harbor's), else the task's prompt-like
                                                # field
        attachments: ["/app/*.png"]             # optional: globs of images in the environment, sent with the question
        answer_file: /app/answer.txt            # where the answer is written in the environment (the default, as the
                                                # Harbor runner's answer agent wrote it; null: nowhere)
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
import os
from pathlib import Path
import re
import tempfile
import time

from hermit.agent.acting.agent import Agent
from hermit.agent.base.deadline import Deadline
from hermit.agent.model.usage import Ledger
from hermit.agent.model.direct import Direct
from hermit.agent.model.llm import ChatAborted, ChatClient, ChatHTTPError
from hermit.common import RunOutput

INPUT_FIELDS = ("prompt", "question", "instruction", "input")
ANSWER_FILE = "/app/answer.txt"                  # where the answer goes in the environment unless answer_file says
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
        client = ChatClient.from_spec(model)
        if model.get("max_retries") is not None:        # the relay forwarded it; the chat client takes it here
            client.max_retries = int(model["max_retries"])
        budget = getattr(getattr(environment, "limits", None), "timeout_s", None) if environment is not None else None
        # the task's agent budget bounds the whole call (the run's one Deadline), as Harbor's agent timeout cancelled
        # the runner's
        caller = Deadline.within(budget).bound(client) if budget else client
        try:
            return self._answer(task, adapter, environment, model, client, caller, budget, started)
        finally:
            if budget:
                caller.close()

    def _answer(self, task, adapter, environment, model, client, caller, budget, started) -> RunOutput:
        p = self.params
        question = self.question(task, environment)
        session = self.session(adapter)
        try:
            system = session.context(query=question, task=task) if session is not None else ""
            record = session.last_context if session is not None else None
        except Exception as exc:          # the adapter's own (its context tool): the agent's result, scored 0
            message = f"{type(exc).__name__}: {exc}"[-2000:]
            return RunOutput(output=None, cost_usd=0.0, tokens=0, wall_s=round(time.time() - started, 1),
                             api_speed={"model_calls": 0},
                             extra={"adapter_failed": "adapter_failed", "exception_message": message,
                                    "session_failed": message[-600:]})
        finally:
            if session is not None:
                session.close()
        images, named = self.attachments(environment)
        content = [{"type": "text", "text": question}, *({"type": "image_url", "image_url": {"url": url}}
                                                        for url in images)] if images else question
        messages = ([{"role": "system", "content": system}] if system.strip() else []) + \
            [{"role": "user", "content": content}]
        timed_out = False
        try:
            # max_tokens only when given (the runner's answer agent sent none otherwise)
            reply = Direct({"max_tokens": int(p["max_tokens"]) if p.get("max_tokens") else None}, caller).run(messages)
        except ChatAborted:
            if not (budget and caller.expired):
                raise
            from hermit.agent.model.direct import Reply
            reply, timed_out = Reply(reply=""), True
        except ChatHTTPError as exc:
            reply, timed_out = self._refused(exc), False
        answer = self.answer(reply.reply)
        answer_file = None if "answer_file" in p and p["answer_file"] is None else p.get("answer_file") or ANSWER_FILE
        timed_out = timed_out or bool(budget and caller.expired)
        if environment is not None and answer_file and not timed_out:
            text = str(p.get("answer_format") or "{answer}").replace("{answer}", answer)
            with tempfile.TemporaryDirectory(prefix="hermit-answer-") as tmp:
                local = Path(tmp) / "answer.txt"
                local.write_text(text, encoding="utf-8")
                if not budget:
                    environment.upload(local, str(answer_file))
                else:                     # delivered whole within the budget, or not at all: put beside, then moved
                    part = f"{answer_file}.hermit-part"
                    environment.upload(local, part)
                    left = caller.remaining()
                    done = environment.exec(["mv", "-f", part, str(answer_file)], timeout=left) if left > 0 else None
                    if done is None or done.timed_out:   # the budget spent first: no answer delivered
                        environment.exec(["rm", "-f", part, *([] if done is None else [str(answer_file)])], timeout=30)
                        timed_out = True
                    elif done.code != 0:
                        raise RuntimeError(f"the task's environment did not provide {answer_file} for the answer: "
                                           f"{done.output[-300:]}")
        logs = self.log_dir()
        fd, name = tempfile.mkstemp(prefix=f"{task.get('id', 'task')}-", suffix=".json", dir=logs)
        log = Path(name)
        shown = [{**m, "content": question if isinstance(m["content"], list) else m["content"]} for m in messages]
        with os.fdopen(fd, "w", encoding="utf-8") as f:   # the descriptor mkstemp opened, closed with the file
            f.write(json.dumps({"task": task.get("id"), "model": model["model"], "messages": shown,
                                "attachments": named, "reply": reply.reply, "answer": answer},
                               ensure_ascii=False, indent=1))
        usage = Ledger(getattr(client, "model", "")).add(reply.usage).unreported(
            caller.abandoned if budget else 0).total()
        extra = {"attachments": named, **({"timed_out": True} if timed_out else {})}
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
                if time.time() >= deadline:      # the environment's: infra, run again (the runner's launch_failed)
                    raise RuntimeError(f"the task's environment did not provide {path} (waited "
                                       f"{self.params.get('wait_s') or DEFAULT_WAIT_S} s)")
                time.sleep(POLL_S)
        if getattr(environment, "instruction", None) is not None:
            return environment.instruction            # Harbor's instruction, as the runner's answer agent was given it
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
            raise RuntimeError(f"the task's environment did not provide its attachments' listing: "
                               f"{listing.output[-500:]}")
        urls, named = [], []
        found = list(dict.fromkeys(line for line in listing.output.splitlines() if line.strip()))   # each image once
        for path in found:
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

    @staticmethod
    def _refused(exc):
        """The model endpoint's refusal, read as the Harbor runner read the answer agent's: a bill or a spent quota ends
        the run (usage_limit), the run's configuration refused (401-404, a request setting it does not take) is
        model_rejected (never run again), a provider that could not serve is infra; a request refused for what its
        input made it (a prompt too long, an invalid request) is the agent's result: no answer."""
        from hermit.agent.base.codex_stream import BILLING_REFUSED, CONFIG_REFUSED, QUOTA_SAID
        from hermit.agent.base.interface import LoginFailed
        from hermit.agent.model.direct import Reply
        said = f"{exc.code} {exc.body.decode(errors='replace')[-1000:]}"
        if exc.code == 402 or BILLING_REFUSED.search(said) or QUOTA_SAID.search(said):
            raise LoginFailed(f"agent answer: usage_limit: the provider does not bill the account: {said}") from None
        if exc.code in (401, 403, 404) or (exc.code == 400 and CONFIG_REFUSED.search(said)):
            raise RuntimeError(f"agent answer: model_rejected: {said}") from None
        if exc.code in (408, 409, 429) or exc.code >= 500:
            raise exc
        return Reply(reply="")

    def answer(self, reply: str) -> str:
        pattern = self.params.get("answer_pattern")
        if not pattern:
            return reply.strip()
        found = list(re.finditer(str(pattern), reply))   # the runner's extract: its last match, group 1 when it has
        if not found:                                    # one and it took part, else the whole match
            return reply.strip()
        last = found[-1]
        return (last.group(1) if last.re.groups and last.group(1) is not None else last.group(0)).strip()
