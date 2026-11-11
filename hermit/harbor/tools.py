"""What a trial's agent reaches on this machine: the adapter's tools, and the answer-only agent's model.

`AdapterTools`: an adapter's agent tools for a CLI in a task container, served from here as Hermit's own sessions in
a container get them (hermit.agent.base.cli: a ToolServer on the container network's gateway): a Session of the
adapter in the strict adapter sandbox (the adapter's code never runs in this process, nor with its keys or network),
its tools behind a ToolServer listening on the Docker bridge's gateway address (`docker network inspect bridge`;
a container on a fresh compose network reaches the host there, checked 2026-11-11), the URL under a random token, each
call in the audit log (`tool-calls.jsonl` in the trial's job directory, which the curated log keeps). The launch plan
names the URL in the CLI's own MCP entry. One server per trial, closed after it; a stream keeps one server for the
stream's life and swaps the Session behind it at each step, whose adapter may have changed (`swap`).

`ModelEndpoint`: the answer-only agent's model, a ChatEndpoint on 127.0.0.1 (the agent runs in Harbor's own process
on this machine) over the `direct` Base Agent of the model's spec: a relay that sends the agent's request on with
the backend's key, which never leaves this process; or, for a model only a CLI reaches, over that CLI's sessions
(cli_endpoint, each in a sandbox of its own). It counts each call from its arrival, what it used (`usage`, priced at
the spec's price, an explicit price included) and how it was answered (`statuses`, `facts`), and for a CLI's model on a
login whether the call's session waited for the login's plan, as that session recorded it (`plan_notes`, through the
relay to the pipeline's CLI endpoint too: hermit.agent.base.endpoint.NOTE_HEADER). Closed after the trial
(or the step), it gives up the calls still in flight (an agent Harbor stopped leaves its request running), whose
tokens would otherwise be spent after the trial's usage was read, and waits for them up to CLOSE_WAIT_S.
"""
from __future__ import annotations

import dataclasses
import json
import subprocess
import threading
from pathlib import Path

from hermit.agent.base.endpoint import ChatEndpoint

_GATEWAY: dict[str, str] = {}
_LOCK = threading.Lock()
CLOSE_WAIT_S = 15.0         # how long a closing ModelEndpoint waits for the calls it gave up to end


def docker_gateway(network: str = "bridge") -> str:
    """The gateway address of a Docker network (the host's address on it), asked once per process."""
    with _LOCK:
        if network not in _GATEWAY:
            try:
                out = subprocess.run(["docker", "network", "inspect", network, "--format",
                                      "{{range .IPAM.Config}}{{.Gateway}} {{end}}"], capture_output=True, text=True,
                                     timeout=60)
            except (OSError, subprocess.SubprocessError) as exc:
                raise RuntimeError(f"hermit: the Docker network {network} could not be inspected for its gateway "
                                   f"({exc}): the adapter's tools are served on it") from None
            found = [a for a in out.stdout.split() if a.count(".") == 3]
            if out.returncode != 0 or not found:
                raise RuntimeError(f"hermit: the Docker network {network} has no IPv4 gateway "
                                   f"({(out.stderr or out.stdout).strip()[-300:]}): the adapter's tools are served on "
                                   "it (or set agent.params.tools_host)")
            _GATEWAY[network] = found[0]
        return _GATEWAY[network]


def adapter_sandbox(enforce: str | None = None):
    """The strict adapter sandbox of a Session (hermit.adapter.session), with the run's sandbox.enforce."""
    from hermit.adapter.session import _adapter_sandbox
    box = _adapter_sandbox()
    return dataclasses.replace(box, enforce=enforce) if enforce else box


class AdapterTools:
    """An adapter's agent tools served to a trial's container (the module's docstring)."""

    def __init__(self, adapter: Path, *, audit_log: Path, host: str | None = None, enforce: str | None = None):
        self.host = host or docker_gateway()
        self.enforce = enforce
        self.server = None
        self.session = self.sandbox = None
        self.adapter, self.audit_log = Path(adapter), Path(audit_log)

    def _open(self, adapter: Path, audit_log: Path):
        from hermit.adapter.session import Session
        box = adapter_sandbox(self.enforce)
        try:
            session = Session(adapter, sandbox=box, audit_log=audit_log)
        except BaseException:
            box.close()
            raise
        return session, box

    def start(self) -> "AdapterTools":
        from hermit.agent.base.tool_server import ToolServer
        self.session, self.sandbox = self._open(self.adapter, self.audit_log)
        try:
            self.server = ToolServer(self.session.as_tools(), host=self.host).start()
        except BaseException:
            self._close_session(self.session, self.sandbox)
            raise
        return self

    @property
    def mcp_url(self) -> str:
        return self.server.mcp_url

    @property
    def names(self) -> list[str]:
        return sorted(self.server.tools)

    def swap(self, adapter: Path, audit_log: Path) -> None:
        """The tools of another adapter (a stream's next step) behind the same URL: the server's tools replaced in
        place (its handlers read them at each call), the old Session closed."""
        if self.server is None:
            raise RuntimeError("hermit: the stream's tool server was closed (its stream stopped)")
        session, box = self._open(Path(adapter), Path(audit_log))
        tools = {t.name: t for t in session.as_tools()}
        old = (self.session, self.sandbox)
        self.server.tools.clear()
        self.server.tools.update(tools)
        self.server.mcp_tools.clear()
        self.server.mcp_tools.update(tools)
        self.session, self.sandbox, self.adapter, self.audit_log = session, box, Path(adapter), Path(audit_log)
        self._close_session(*old)

    @staticmethod
    def _close_session(session, box) -> None:
        try:
            if session is not None:
                session.close()
        finally:
            if box is not None:
                box.close()

    def close(self) -> None:
        try:
            if self.server is not None:
                self.server.close()
        finally:
            self.server = None
            self._close_session(self.session, self.sandbox)
            self.session = self.sandbox = None


class ModelEndpoint:
    """The answer-only agent's model on 127.0.0.1 (the module's docstring). `spec` is a model spec (a Binding's: the
    `direct` Base Agent relays each request to the backend, with the spec's client `timeout` and `max_retries`), or a
    model only a CLI reaches (`{cli: codex | claude | pi, model, login, ...}`: each request one session of that CLI, in
    a sandbox of its own, hermit.isolate.cli_sandbox, as a run's `llm: {cli: ...}`; its event streams kept under
    `records`). `plan_notes`: the model is a CLI's on a login, here or behind the CLI endpoint of this process the spec
    relays to (hermit.isolate.agent_pipeline's), whose session records each call's waits for its plan (`facts`)."""

    def __init__(self, spec: dict, *, host: str = "127.0.0.1", records: Path | None = None,
                 enforce: str | None = None, plan_notes: bool = False):
        self.spec, self.host = dict(spec), host
        self.records = Path(records) if records is not None else None
        self.enforce = enforce
        self.plan_notes = plan_notes
        self.client = self.endpoint = None
        self.lock = threading.Condition()                  # the calls' records; notified as each call ends
        self.running: set = set()                          # the calls in flight, by their own abort event
        self.aborted = 0                                   # calls given up before their reply
        self.closing = False
        self.statuses: list = []
        self.codes: list = []
        self.messages: list = []                           # each call's error message (its last 1000 characters)
        self.used: list[dict] = []                         # each answered call's usage (a CLI session's reply)
        self.notes: list[dict] = []                        # each call's waits for its plan, by arrival (plan_notes)

    def start(self) -> "ModelEndpoint":
        if self.spec.get("cli"):
            from hermit.agent.base.endpoint import cli_endpoint
            from hermit.isolate import cli_sandbox
            box, proxy = cli_sandbox(self.spec, records=self.records, enforce=self.enforce or "required",
                                     role="agent-model")
            try:
                endpoint = cli_endpoint(self.spec, self.records / "model-sessions" if self.records else None,
                                        sandbox=box)
            except BaseException:
                for thing in (box, proxy):
                    if thing is not None:
                        thing.close()
                raise
            endpoint.closing += [t for t in (box, proxy) if t is not None]
        else:
            from hermit.agent.base.interface import make
            from hermit.agent.model.llm import ChatClient
            # its own retries for a provider that could not serve (the spec's max_retries, when it gives them)
            given = self.spec.get("max_retries")
            self.client = ChatClient.from_spec(self.spec, **({} if given is None else {"max_retries": int(given)}))
            endpoint = ChatEndpoint(make("direct", {}, self.client), host=self.host).start()
        answer = endpoint.answer

        def metered(request, authorization, abort=None, note=None):
            # each call from its arrival (in flight under its own abort event, which close sets), then its status, a
            # failed one's error code and message; with plan_notes, the record its session keeps of its waits for the
            # plan, the endpoint's own (a record the request named, NOTE_HEADER, is not taken)
            abort = abort if abort is not None else threading.Event()
            mine = {} if self.plan_notes else None
            with self.lock:
                self.running.add(abort)
                if mine is not None:
                    self.notes.append(mine)
                if self.closing:                             # it came as the endpoint closed: given up at once
                    abort.set()
            reply = None
            try:
                reply = answer(request, authorization, abort, mine)
            finally:
                self._note(reply, abort)
            return reply
        endpoint.answer = metered
        self.endpoint = endpoint
        return self

    def _note(self, reply, call: threading.Event | None = None) -> None:
        """A call's end: its reply, and the call (its abort event) no longer in flight; one given up before its reply
        (no reply while its abort was set) is counted apart, its use unknown."""
        status = reply[0] if isinstance(reply, tuple) else None           # None: no answer (the upstream's)
        code, used, message = None, None, None
        if isinstance(reply, tuple):
            try:
                body = json.loads(reply[1])
            except (ValueError, TypeError, RecursionError):  # no JSON, or nested past the parser's depth
                body = {}
            body = body if isinstance(body, dict) else {}
            error = body.get("error")
            code = error.get("code") if status != 200 and isinstance(error, dict) else None
            if status != 200:       # why it failed: a CLI session's 400 says it only here (endpoint._no_reply); its
                # last 1000 characters: the whole of what that reply carries ("the <cli> session gave no reply (exit
                # 1): " and the last 600 characters of the CLI's failure, whose leading status stays readable), the
                # end of a longer message. A body whose error has no message may say it at its top level (Mistral's
                # `message`, a FastAPI server's `detail`)
                said = error.get("message") if isinstance(error, dict) else error
                if not said:
                    said = next((body[k] for k in ("message", "detail") if isinstance(body.get(k), str) and body[k]),
                                None)
                message = str(said)[-1000:] if said else None
            used = body.get("usage") if isinstance(body.get("usage"), dict) else None
        with self.lock:
            self.statuses.append(status)
            self.codes.append(code)
            self.messages.append(message)
            if used is not None:
                self.used.append(used)
            if call is not None:
                self.running.discard(call)
                self.aborted += int(call.is_set() and not isinstance(reply, tuple))
                self.lock.notify_all()

    @property
    def url(self) -> str:
        return self.endpoint.url

    @property
    def usage(self):
        """What the calls used (hermit.agent.model.usage.Usage): the relay's client's total, priced at the spec's price;
        a CLI's sessions' replies, priced the same way (none on a login). A call given up before its reply, or still
        in flight when the endpoint closed, is among its calls as one whose tokens no reply reported
        (`unreported_calls`): the cost is then unknown (None), never what the other calls cost."""
        from hermit.agent.base.records import price_usage
        from hermit.agent.model.usage import Usage
        with self.lock:
            used, uncounted = list(self.used), self.aborted + len(self.running)
        if self.client is not None:
            total = dataclasses.replace(self.client.total)
        else:
            total = Usage(model=str(self.spec.get("model") or self.spec.get("cli")))
            for u in used:
                total.input_tokens += int(u.get("prompt_tokens") or 0)
                total.output_tokens += int(u.get("completion_tokens") or 0)
                total.cached_input_tokens += int((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
            total.calls = len(used)
        total.calls += uncounted
        total.unreported_calls += uncounted
        if self.client is None:
            price_usage([total], self.spec)
        if uncounted:
            total.cost_usd = None
        return total

    def facts(self) -> dict:
        """The session's facts, as a CLI's stream gives them: served (a call answered), unavailable (the last call got
        no answer, or a status a retry may change), refused (a CLI session the plan refused: 429), login_failed (a
        CLI-reached model's sign-in lost: the endpoint's code), rejected (a status no retry changes), code and failure
        (the last call's error code and message); started: a call was made, counted from its arrival (a request still
        in flight when the agent's time ran out was made); usage_incomplete: a call was given up before its reply, or
        was still in flight when the endpoint closed, so what it used is not in `usage`; with plan_notes, plan_waited
        (why the plan held the last call's session back, as that session met it: login.wait; "" when it never waited)
        and plan_waiting (it was still waiting when it was read: the call cut off, or given up, while it waited)."""
        from hermit.agent.base.interface import LOGIN_FAILED
        with self.lock:
            statuses, codes, messages = list(self.statuses), list(self.codes), list(self.messages)
            running, aborted = len(self.running), self.aborted
            note = dict(self.notes[-1]) if self.notes else {}
        last = statuses[-1] if statuses else 0
        served = 200 in statuses
        lost = not served and bool(codes) and codes[-1] == LOGIN_FAILED
        refused = not served and bool(self.spec.get("cli")) and last == 429
        busy = not refused and (last is None or last in (408, 409, 429) or (isinstance(last, int) and last >= 500))
        calls = len(statuses) + running
        waits = {"plan_waited": str(note.get("waited") or ""), "plan_waiting": bool(note.get("waiting"))} \
            if self.plan_notes else {}
        return {"served": served, "unavailable": not served and bool(statuses) and busy and not lost,
                "refused": refused, "login_failed": lost, "started": calls > 0, "calls": calls,
                "rejected": not served and not lost and isinstance(last, int) and 400 <= last < 500 and not busy
                and not refused, "statuses": statuses, "code": codes[-1] if codes else None,
                "failure": (messages[-1] if messages else None) or "", "usage_incomplete": bool(running or aborted),
                **waits}

    def close(self) -> None:
        """The endpoint closed, after the calls still in flight were given up (each one's abort: a relay's call is
        dropped upstream, ChatAborted; a CLI's session is cancelled) and waited for, up to CLOSE_WAIT_S, so that what
        they recorded is in the facts and the usage read after it; then its listener, and what goes with it (a CLI's
        sandbox)."""
        if self.endpoint is None:
            return
        with self.lock:
            self.closing = True
            for call in self.running:
                call.set()
            self.lock.wait_for(lambda: not self.running, timeout=CLOSE_WAIT_S)
        self.endpoint.close()
        self.endpoint = None
