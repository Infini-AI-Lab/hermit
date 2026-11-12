"""HermitAnswer: an acting agent that only answers, one request to Hermit's model, its answer written into the task.

Imported by Harbor as `harborside.answer:HermitAnswer`, for a task whose work is a reply (a question with a figure,
a value from a document) rather than a session in the container. It is an external agent: it runs in Harbor's own
process on the host, outside the task container's network policy, and installs nothing there. Its launch plan
(`cli: answer`, written by hermit.harbor.launch) has no command but an `answer` block:

    endpoint          Hermit's chat endpoint for the trial (http://127.0.0.1:<port>/<token>), which serves the run's
                      model with Hermit's binding and prices (a direct model, or a CLI-reached one)
    model, system     the request's model, and its system message (the adapter's per-task context; may be empty)
    instruction_file  null: Harbor's instruction is the question; else a file in the container that holds it (a task
                      whose entrypoint writes it, such as LAB-Bench's /app/instruction.md), waited for up to wait_s
    attachments       globs of files in the container sent with the question as image data URLs (base64 over exec),
                      each image they match but one over MAX_ATTACHMENT_BYTES, left out with a warning
    answer_file       where the answer is written in the container, as the task's agent user (base64 through exec,
                      so no quoting of the text is involved); null: written nowhere
    answer_pattern    a regex: the answer is its last match in the reply (group 1 when it has one), else the reply
    answer_format     the text written, with `{answer}` in it (the tagged form a verifier reads)
    max_tokens, effort   the request's max_tokens and reasoning_effort, when given

One POST <endpoint>/chat/completions (the standard library's urllib, in a thread of its own, so that Harbor's timeout
can stop the trial; an endpoint on this machine is reached directly, never through the http(s)_proxy of Harbor's
environment). `/logs/agent/answer.json` keeps {reply, answer, usage, model, attachments} (the attachments' paths,
never their bytes, and never the endpoint, whose path is its token). A model that failed (an HTTP error, no reply, no
connection) raises NonZeroAgentExitCodeError, so the verifier still runs; what the task's container did not provide
(no instruction file in time, attachments whose listing exec failed, a question or a figure that cannot be read there:
no base64 or tr, a file the agent user may not open, no wc for a figure's size; no answer file written) and what this
machine could not do with the model's answer (keep it in answer.json; run the exec that writes it: an OSError, the
compose client not spawned) raise HermitLaunchError, which Hermit's side counts as infra (harborside.hermit). A figure
a wildcard would match in a directory the agent user may not list is not found, as by a glob that matches nothing (no
error: the model is asked without it); one named outright is found wherever the agent user may enter its directory.
The agent declares no skills or MCP servers, so a task that names some is refused by Harbor, saying so, rather than
answered without them.
"""
from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
from pathlib import Path, PurePosixPath
import re
import shlex
import threading
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from harbor.agents.base import BaseAgent
from harbor.agents.capabilities import AgentCapabilities
from harbor.agents.installed.base import NonZeroAgentExitCodeError
from harbor.agents.options import AgentOptions
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from pydantic import Field, field_validator

from harborside.hermit import HermitLaunchError, load_plan, marked

# What is read from the container is printed after a mark on a line of its own (harborside.hermit.marked): Harbor's
# docker environment merges an exec's stderr into its stdout
INSTRUCTION_LINE = "hermit-instruction:"          # the instruction file, in base64
FILE_LINE = "hermit-file:"                        # an attachment's path, one line each
SIZE_LINE = "hermit-size:"                        # its size in bytes
DATA_LINE = "hermit-data:"                        # its bytes, in base64
UNREADABLE = 3                                    # a read's exit code: the file cannot be read in base64 here
POLL_S = 1.0
DEFAULT_WAIT_S = 30.0
REQUEST_TIMEOUT_S = 3600                          # one request at most; Harbor's agent timeout stops the trial before
MAX_ATTACHMENT_BYTES = 20 * 2**20                 # one attachment's bytes at most (it is sent inline)
CHUNK = 60_000                                    # base64 characters per exec when the answer is written
GLOB = re.compile(r"/[A-Za-z0-9_./*?\[\]+@%:,=-]*")   # a container glob that needs no quoting to expand


def answer_problems(spec: Any) -> list[str]:
    """What is wrong with a plan's `answer` block, as messages naming the field; [] when none."""
    if not isinstance(spec, dict):
        return ["answer must be a mapping (the answer agent's plan)"]
    problems = []
    if not str(spec.get("endpoint") or "").startswith(("http://", "https://")):
        problems.append("answer.endpoint must be an http(s) URL")
    if not isinstance(spec.get("model"), str) or not spec["model"]:
        problems.append("answer.model must name the model")
    if not isinstance(spec.get("system") or "", str):
        problems.append("answer.system must be text")
    for key in ("instruction_file", "answer_file"):
        if spec.get(key) is not None and not str(spec[key]).startswith("/"):
            problems.append(f"answer.{key} must be an absolute container path or null")
    wait = spec.get("wait_s", DEFAULT_WAIT_S)
    if isinstance(wait, bool) or not isinstance(wait, (int, float)) or wait < 0:
        problems.append("answer.wait_s must be a number of seconds")
    for pattern in spec.get("attachments") or []:
        if not isinstance(pattern, str) or not GLOB.fullmatch(pattern):
            problems.append(f"answer.attachments must be absolute globs of plain characters, not {pattern!r}")
    if spec.get("answer_pattern") is not None:
        try:
            re.compile(str(spec["answer_pattern"]))
        except re.error as exc:
            problems.append(f"answer.answer_pattern is not a regex: {exc}")
    if "{answer}" not in str(spec.get("answer_format") or "{answer}"):
        problems.append("answer.answer_format must hold {answer}")
    tokens = spec.get("max_tokens")
    if tokens is not None and (isinstance(tokens, bool) or not isinstance(tokens, int) or tokens <= 0):
        problems.append("answer.max_tokens must be a positive integer or null")
    return problems


def extract(reply: str, pattern: str | None) -> str:
    """The answer in a reply: the last match of `pattern` (its group 1 when it has one), else the whole reply."""
    if pattern:
        matches = list(re.finditer(pattern, reply))
        if matches:
            last = matches[-1]
            found = last.group(1) if last.re.groups and last.group(1) is not None else last.group(0)
            return found.strip()
    return reply.strip()


def _read_base64(path: str, mark: str) -> str:
    """The command that prints the file at `path` in base64 on a line after `mark`, as the context capture of
    harborside.hermit reads the image's own file: base64 and tr checked first, the read under pipefail, so that a
    file it cannot read exits UNREADABLE saying why (without them, `base64 < f | tr -d '\\n'` succeeds with nothing)
    and is never an empty value."""
    q = shlex.quote(path)
    return ("command -v base64 >/dev/null 2>&1 && command -v tr >/dev/null 2>&1 || "
            f"{{ echo 'hermit: no base64 or tr in the image' >&2; exit {UNREADABLE}; }}; "
            f"v=$(set -o pipefail; base64 < {q} | tr -d '\\n') || exit {UNREADABLE}; printf '{mark}%s\\n' \"$v\"")


def _text(content: Any) -> str:
    """A chat message's content as text (a string, or the text of its parts)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(p.get("text") or "") for p in content if isinstance(p, dict) and p.get("type") in
                       (None, "text", "output_text"))
    return ""


def _opener(url: str) -> urllib.request.OpenerDirector:
    """urllib's opener for the endpoint: without proxies for one on this machine (Hermit's endpoint is; a proxy from
    the environment would not reach it), the environment's otherwise."""
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if host in ("localhost", "::1") or host.startswith("127."):
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


def _request(url: str, body: dict) -> dict:
    """One chat completion; the parsed response, or NonZeroAgentExitCodeError (never naming the URL: its path is the
    endpoint's token)."""
    data = json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})
    try:
        with _opener(url).open(request, timeout=REQUEST_TIMEOUT_S) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        said = (error.read() or b"").decode("utf-8", errors="replace")[:600]
        raise NonZeroAgentExitCodeError(f"hermit: the answer's model failed: HTTP {error.code}: {said}") from None
    except (urllib.error.URLError, OSError) as error:
        reason = getattr(error, "reason", error)
        raise NonZeroAgentExitCodeError(f"hermit: the answer's model was not reached: {reason}") from None
    try:
        answer = json.loads(raw or b"null")
    except ValueError:
        raise NonZeroAgentExitCodeError("hermit: the answer's model sent no JSON response") from None
    if not isinstance(answer, dict):
        raise NonZeroAgentExitCodeError("hermit: the answer's model sent no JSON object")
    return answer


class HermitAnswerOptions(AgentOptions):
    """The answer agent's kwargs: its plan. `extra="forbid"`."""

    plan: str | None = Field(default=None, description="The launch plan Hermit rendered for the trial (cli: answer).")

    @field_validator("plan")
    @classmethod
    def _plan_is_a_file(cls, value: str | None) -> str | None:
        if value is not None and not Path(value).expanduser().is_file():
            raise ValueError(f"the launch plan {value} is not a file")
        return value


class HermitAnswer(BaseAgent):
    capabilities = AgentCapabilities()            # no skills, no MCP servers: Harbor refuses such a task, saying so
    options_model = HermitAnswerOptions
    options: HermitAnswerOptions

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._plan: dict | None = None
        if self.options.plan:
            self.set_plan(self.options.plan)

    @staticmethod
    def name() -> str:
        return "hermit-answer"

    def version(self) -> str:
        return "1"

    def set_plan(self, path: str | None) -> None:
        """The plan the next `run` follows (a stream's driver gives each step its own); None: no plan."""
        if not path:
            self._plan = None
            return
        plan = load_plan(path, "answer")
        problems = answer_problems(plan.get("answer"))
        if problems:
            raise ValueError(f"hermit: the launch plan {path} is not usable: " + "; ".join(problems))
        self._plan = plan

    async def setup(self, environment: BaseEnvironment) -> None:
        """Nothing in the container: the agent runs in Harbor's process."""

    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        if self._plan is None:
            raise HermitLaunchError("hermit: HermitAnswer answers from the launch plan Hermit renders for each trial "
                                    "(kwargs.plan), and it was given none")
        spec = self._plan["answer"]
        question = await self._question(environment, spec, instruction) + str(self._plan.get("prompt_suffix") or "")
        images = await self._attachments(environment, spec)
        content: Any = question if not images else [{"type": "text", "text": question}, *(
            {"type": "image_url", "image_url": {"url": url}} for _, url in images)]
        messages = ([{"role": "system", "content": spec["system"]}] if spec.get("system") else []) + \
            [{"role": "user", "content": content}]
        body: dict = {"model": spec["model"], "messages": messages}
        if spec.get("max_tokens"):
            body["max_tokens"] = int(spec["max_tokens"])
        if spec.get("effort"):
            body["reasoning_effort"] = str(spec["effort"])
        response = await self._post(str(spec["endpoint"]).rstrip("/") + "/chat/completions", body)
        choices = response.get("choices") or []
        message = choices[0].get("message") if choices and isinstance(choices[0], dict) else None
        if not isinstance(message, dict):
            raise NonZeroAgentExitCodeError("hermit: the answer's model sent no message")
        reply = _text(message.get("content"))
        answer = extract(reply, spec.get("answer_pattern"))
        written = str(spec.get("answer_format") or "{answer}").replace("{answer}", answer)
        usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
        answer_json = self.logs_dir / "answer.json"
        try:
            self.logs_dir.mkdir(parents=True, exist_ok=True)
            answer_json.write_text(json.dumps(
                {"reply": reply, "answer": answer, "usage": usage, "model": response.get("model") or spec["model"],
                 "attachments": [path for path, _ in images]}, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        except OSError as exc:                            # this machine's (a full disk, its quota): not the agent's 0
            raise HermitLaunchError(f"hermit: the answer could not be kept in {answer_json}: {type(exc).__name__}: "
                                    f"{exc}") from exc
        if spec.get("answer_file"):
            await self._write(environment, str(spec["answer_file"]), written)
        details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
        context.n_input_tokens = int(usage.get("prompt_tokens") or 0)
        context.n_output_tokens = int(usage.get("completion_tokens") or 0)
        context.n_cache_tokens = int(details.get("cached_tokens") or 0)
        context.metadata = {**(context.metadata or {}), **(self._plan.get("metadata") or {}), "hermit_cli": "answer",
                            "answer_file": spec.get("answer_file"), "attachments": len(images),
                            "instruction_from": "file" if spec.get("instruction_file") else "harbor"}

    async def _question(self, environment: BaseEnvironment, spec: dict, instruction: str) -> str:
        """Harbor's instruction, or the task's instruction file once it is there (polled up to wait_s); a file there
        that cannot be read (no base64 or tr in the image, a read that failed) is HermitLaunchError at once, never an
        empty question."""
        path = spec.get("instruction_file")
        if not path:
            return instruction
        deadline = time.monotonic() + float(spec.get("wait_s", DEFAULT_WAIT_S))
        while True:
            got = await environment.exec(command=f"test -f {shlex.quote(path)} || exit 1; "
                                                 + _read_base64(path, INSTRUCTION_LINE))
            value = marked(got.stdout, INSTRUCTION_LINE) if got.return_code == 0 else None
            if value is not None:
                return base64.b64decode(value).decode("utf-8", errors="replace")
            if got.return_code in (0, UNREADABLE):
                raise HermitLaunchError(f"hermit: the task's instruction file {path} could not be read: "
                                        f"{(got.stdout or got.stderr or '').strip()[-300:]}")
            if time.monotonic() >= deadline:
                raise HermitLaunchError(f"hermit: the task's instruction file {path} was not there after "
                                        f"{spec.get('wait_s', DEFAULT_WAIT_S)} s (its environment writes it): no "
                                        "question to answer")
            await asyncio.sleep(POLL_S)

    async def _attachments(self, environment: BaseEnvironment, spec: dict) -> list[tuple[str, str]]:
        """The attachments' files as (path, image data URL), in the order of their globs, each once: the images among
        them, but one over MAX_ATTACHMENT_BYTES, which is left out with a warning (as a file that is no image is).
        A listing whose exec failed (which files the globs match is not known) and an image that cannot be measured or
        read (no wc, base64 or tr in the image, a file the agent user may not open, an exec that failed) are
        HermitLaunchError: the model is never asked without them. A figure a wildcard would match in a directory the
        agent user may not list is not found, as by a glob that matches nothing (no error: the model is asked without
        it); one named outright is found wherever the agent user may enter its directory."""
        patterns = spec.get("attachments") or []
        if not patterns:
            return []
        listed = await environment.exec(command="shopt -s nullglob; for f in " + " ".join(patterns) +
                                        f"; do [ -f \"$f\" ] && printf '{FILE_LINE}%s\\n' \"$f\"; done; true")
        if listed.return_code != 0:                       # the command ends in `true`: the exec itself failed
            raise HermitLaunchError(f"hermit: the attachments {' '.join(patterns)} could not be listed: "
                                    f"{(listed.stdout or listed.stderr or '').strip()[-300:]}")
        paths = list(dict.fromkeys(line.split(FILE_LINE, 1)[1] for line in (listed.stdout or "").splitlines()
                                   if FILE_LINE in line))
        out = []
        for path in paths:
            kind = mimetypes.guess_type(path)[0] or ""
            if not kind.startswith("image/"):
                self.logger.warning("hermit: %s is no image: it is not sent", path)
                continue
            size = await environment.exec(command=f'n=$(wc -c < {shlex.quote(path)}) && echo "{SIZE_LINE}$n"')
            n = marked(size.stdout, SIZE_LINE) if size.return_code == 0 else None
            if n is None or not n.isdecimal():
                raise HermitLaunchError(f"hermit: the attachment {path} could not be measured: "
                                        f"{(size.stdout or size.stderr or '').strip()[-300:]}")
            if int(n) > MAX_ATTACHMENT_BYTES:
                self.logger.warning("hermit: %s is over %d bytes: it is not sent", path, MAX_ATTACHMENT_BYTES)
                continue
            data = await environment.exec(command=_read_base64(path, DATA_LINE))
            value = marked(data.stdout, DATA_LINE) if data.return_code == 0 else None
            if value is None:
                raise HermitLaunchError(f"hermit: the attachment {path} could not be read: "
                                        f"{(data.stdout or data.stderr or '').strip()[-300:]}")
            out.append((path, f"data:{kind};base64,{value}"))
        return out

    async def _post(self, url: str, body: dict) -> dict:
        """_request in a daemon thread: Harbor's timeout cancels the wait, and the thread holds no exit."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()

        def settle(result, error) -> None:
            if not future.done():
                future.set_exception(error) if error is not None else future.set_result(result)

        def work() -> None:
            try:
                result, error = _request(url, body), None
            except BaseException as exc:                   # noqa: BLE001 - handed to the waiting coroutine
                result, error = None, exc
            try:
                loop.call_soon_threadsafe(settle, result, error)
            except RuntimeError:                           # the loop is gone: the trial no longer waits
                pass

        threading.Thread(target=work, daemon=True, name="hermit-answer").start()
        return await future

    async def _write(self, environment: BaseEnvironment, path: str, text: str) -> None:
        """The answer into the container as the task's agent user: base64 through exec, a chunk at a time. An exec
        that exited non-zero, or that this machine could not run (an OSError: the compose client not spawned, EMFILE,
        EAGAIN), is HermitLaunchError."""
        encoded = base64.b64encode(text.encode("utf-8")).decode()
        chunks = [encoded[i:i + CHUNK] for i in range(0, len(encoded), CHUNK)] or [""]
        target = shlex.quote(path)
        parent = shlex.quote(str(PurePosixPath(path).parent))
        for i, chunk in enumerate(chunks):
            into = ">" if i == 0 else ">>"
            command = (f"mkdir -p {parent} && printf '%s' {shlex.quote(chunk)} | base64 -d {into} {target}"
                       if chunk else f"mkdir -p {parent} && : > {target}")
            try:
                result = await environment.exec(command=command)
            except OSError as exc:                        # this machine's: the compose client not spawned (EMFILE)
                raise HermitLaunchError(f"hermit: the answer could not be written to {path}: its exec could not be "
                                        f"run here: {type(exc).__name__}: {exc}") from exc
            if result.return_code != 0:
                raise HermitLaunchError(f"hermit: the answer could not be written to {path} as the task's agent "
                                        f"user: {(result.stdout or result.stderr or '').strip()[-300:]}")
