"""AHE-Beta's own simulated user (its checkout's `ahe_beta.integrations.hermit.session`) behind user.py's simulate
contract, its model reached through Hermit.

    user:
      simulate: ahe_user:simulate
      pythonpath: [$AHE_ROOT/backend]          # the AHE-Beta checkout (pinned in ../README.md)
      assignments: ../cohort/assignments.json  # each row's persona, scenario and round (prepare.py), or task_field
      params:
        run_id: u8-codex                       # the session's provenance in AHE-Beta's records
        model: {cli: codex, model: gpt-5.6-luna, login: chatgpt, effort: low, unapplied: [temperature],
                shared_plan: true}             # or {ref: <backend>/<model>, effort: ...}

The user's model is Layer 1 (docs/framework/agent-layers.md): a `<backend>/<model>` reference is reached through a
local relay endpoint (one Direct call a request: hermit.agent.base.endpoint.relay), the backend's key passed on (a
placeholder for a backend without one, never the ambient OPENAI_API_KEY AHE-Beta's client would fall back to);
`{cli: ...}` is a chat endpoint over that CLI, each request one session in a bubblewrap sandbox of its own that reaches
only the login's hosts (hermit.isolate.cli_sandbox), as an acting pipeline's `model: {cli: ...}` runs; `unapplied`
names what AHE-Beta's client asks that a CLI session does not apply. With `shared_plan` (prepare.py sets it when the
actor is on the same login) those sessions count as the rollout's: the run already admitted the task's environment to
the login's plan (its wait and its slot, hermit.isolate), so a session of the user's neither waits for the plan nor
takes a slot of its own, which with SLOTS at or under the agent's concurrency would wait for a slot only a finished
rollout frees; without it each session is admitted as any session on the login is. AHE-Beta's client reads its model
from USER_LLM_BASE_URL, USER_LLM_API_KEY, USER_LLM_MODEL and USER_LLM_REASONING_EFFORT, and records its usage where
AHE_BETA_USAGE_PATH says (the run's private folder, not the checkout); they are set here, once for the process (every
session of a run has the same user model). A model that cannot serve as configured raises Misconfigured, a final
failure (no run again). Each session's usage is returned with its `tokens` and `cost_usd` (from the reference's price;
None without one), which user.py adds to the rollout's.

What a learner reads of AHE-Beta's verdict (user.py's VISIBLE) is mapped here: the rating, its structured `feedback`
(the task assessment, the experience and its reason, the attribution, the opportunities it raises as `issues`) and the
outcome's end reason and terminal note. Its full `user_outcome`, which in a session without interaction holds the
review's call traces (their system prompt is the persona), stays private (files/outcome.json), as does its session
record.

AHE-Beta's provenance (`run_id`, `harness_sha`) comes from the run: `harness_sha` is the adapter's digest that user.py
hands over (`adapter_sha256`), or, with AHE-Beta's own Evolver (`params.binding`, ../delivery/), the delivery commit
its binding names for that adapter, which AHE-Beta's feedback import requires each session to carry. An
issue the review leaves untitled is titled with the scenario's `task_key`, else the persona's identity: the task's
public name is given as its `task_key`.
"""
from __future__ import annotations

import atexit
import contextlib
import copy
import json
import os
from pathlib import Path
import re
import threading

_LOCK = threading.Lock()
_MODEL: dict | None = None           # the user model's variables, once set
_PRICE: dict | None = None           # its price (USD per million tokens), for a reference that has one
_CLOSE: list = []
NO_KEY = "hermit-no-key"             # the key a backend without one is sent: a placeholder, never a real one


class SignInLost(RuntimeError):
    """The user model's sign-in cannot serve (Hermit's endpoint answered 401): no session runs until it is restored,
    so the run ends (its text carries Hermit's mark, which the rollout reads) instead of buying more sessions."""
    final = True


_SIGN_IN_LOST = threading.Event()        # set by the endpoint's answer; one sign-in serves the process's sessions


class NotBound(RuntimeError):
    """The adapter a session runs is not the one AHE-Beta's own Evolver bound to its delivery commit (its binding):
    every session of the round would say so, so no run again does better (user.py: final)."""
    final = True


class Misconfigured(ValueError):
    """The user's model as configured cannot serve (a key not set, a dialect, an option the relay drops): no run
    again does better (user.py: final)."""
    final = True
# AHE-Beta's issue artifact ends with a line of metadata for its developers (user/sim/loop.py, emit_issue_artifact): the
# persona's id, its capability tier and correction style. Private: cut from what a learner reads.
FOOTER = re.compile(r"(?:\s*-{3,}[ \t]*)?\s*_auto-collected\b[^\n]*")


def _model_env(spec, records: Path, admitted: bool = False) -> dict:
    """USER_LLM_* for AHE-Beta's client: a <backend>/<model> reference's URL, key and model id, or a chat endpoint over
    a CLI (`{cli: ...}`), started once for the process and closed at its exit. `admitted`: the run holds the login's
    plan admission for the task's environment (user.py's `plan_admitted`), which `shared_plan` shares."""
    if isinstance(spec, dict) and spec.get("cli"):
        from hermit.agent.base.endpoint import cli_endpoint, cli_problems
        from hermit.isolate import cli_sandbox
        settings = {"web_search": False, **{k: v for k, v in spec.items() if k != "shared_plan"}}
        if settings.get("login") and not settings.get("model"):
            raise Misconfigured("user.params.model: a login needs the CLI's model name")
        problems = cli_problems(settings)
        if problems:                                     # before any sandbox or proxy is opened
            raise Misconfigured("user.params.model: " + "; ".join(problems))
        records.mkdir(parents=True, exist_ok=True)
        proxy = None
        try:
            box, proxy = cli_sandbox(settings, records=records, role="user-model")
            endpoint = cli_endpoint(settings, records / "sessions", sandbox=box)
        except Exception as exc:
            if proxy is not None:
                with contextlib.suppress(Exception):
                    proxy.close()
            if isinstance(exc, ValueError):              # the CLI's or the sandbox's own refusal: no run mends it
                raise Misconfigured(f"user.params.model: {exc}") from exc
            raise
        answer = endpoint.answer

        def answered(*args, **kwargs):                   # a lost sign-in, which the session's failure then names
            reply = answer(*args, **kwargs)
            if isinstance(reply, tuple) and reply and reply[0] == 401:
                _SIGN_IN_LOST.set()
            return reply
        endpoint.answer = answered
        if spec.get("shared_plan") and admitted:
            place = endpoint.place

            def place_admitted(work):                    # the rollout holds the login's plan admission (docstring)
                environment = place(work)
                environment.plan_admitted = True
                return environment
            endpoint.place = place_admitted
        _CLOSE.append(endpoint.close)
        if proxy is not None:
            _CLOSE.append(proxy.close)
        return {"USER_LLM_BASE_URL": endpoint.url, "USER_LLM_API_KEY": "hermit-local-endpoint",
                "USER_LLM_MODEL": str(settings.get("model") or settings["cli"]),
                "USER_LLM_REASONING_EFFORT": str(settings.get("effort") or "")}   # never an inherited one
    ref, effort = (spec.get("ref"), spec.get("effort")) if isinstance(spec, dict) else (spec, None)
    if isinstance(ref, str) and ref:
        from hermit.agent.base.endpoint import relay
        from hermit.agent.model.backends import as_binding, problems_of, registry
        bound = as_binding(ref, registry())
        if bound is None:
            raise Misconfigured(f"user.params.model {ref!r} is neither a <backend>/<model> reference nor {{cli: ...}}")
        problems = problems_of(bound, "chat") + ([
            f"its extra_body {sorted(bound.extra_body)} would not reach the model (the relay passes AHE-Beta's "
            "client's own requests on)"] if bound.extra_body else [])
        if problems:                                     # a key a backend needs, its dialect: refused, not retried
            raise Misconfigured(f"user.params.model {ref!r}: " + "; ".join(problems))
        global _PRICE
        _PRICE = bound.price or None
        url, key = relay(bound.base_url), bound.key() or NO_KEY
        refused = _refused(bound.base_url, key)
        if refused:                                      # each session's review would meet it, after its actor ran,
            raise Misconfigured(f"user.params.model {ref!r}: its endpoint refuses the run's key ({refused})")   # again
        return {"USER_LLM_BASE_URL": url, "USER_LLM_API_KEY": key, "USER_LLM_MODEL": bound.model,
                "USER_LLM_REASONING_EFFORT": str(effort or "")}
    if not spec and os.environ.get("USER_LLM_BASE_URL") and os.environ.get("USER_LLM_MODEL"):   # AHE-Beta's own
        if not os.environ.get("USER_LLM_API_KEY"):       # variables, set by the caller: their own key, else its
            raise Misconfigured("USER_LLM_BASE_URL is set without USER_LLM_API_KEY: AHE-Beta's client would send "
                                "OPENAI_API_KEY there (any value for a keyless endpoint)")   # client sends OpenAI's
        return {}
    raise Misconfigured("user.params.model names the user's model: a <backend>/<model> reference or {cli: ...}")


def _loopback_direct() -> None:
    """This machine's own addresses named in NO_PROXY for the process: AHE-Beta's persona clients (httpx) and the key
    probe (urllib) send a proxy every host it does not name, the loopback relay too, with the user's prompts and the
    key in clear (and a proxy cannot reach it)."""
    present = os.environ["no_proxy"] if "no_proxy" in os.environ else os.environ.get("NO_PROXY", "")   # as urllib
    names = [n.strip() for n in present.split(",") if n.strip()]                                   # reads it
    merged = present.strip() if present.strip() == "*" else ",".join(   # "*": every host already
        names + [h for h in ("127.0.0.1", "localhost", "::1") if h not in names])
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = merged


def _refused(base_url: str, key: str) -> int | None:
    """401 when a reference's endpoint refuses the key to its model list, else None (a 403 may refuse the list alone,
    a redirect is not followed with the key, any other answer or none: a session finds out). No model call (nothing
    billed); asked before a process's first session, where the relay sends the same key."""
    from urllib.error import HTTPError
    from urllib.request import HTTPRedirectHandler, Request, build_opener

    class Stay(HTTPRedirectHandler):                     # urllib would carry the key to wherever it is sent
        def redirect_request(self, *args, **kwargs):
            return None
    request = Request(base_url.rstrip("/") + "/models", headers={"Authorization": f"Bearer {key}"})
    try:
        with build_opener(Stay()).open(request, timeout=60):   # this machine's proxy, as the relay's client
            return None
    except HTTPError as exc:
        return exc.code if exc.code == 401 else None
    except (OSError, ValueError):
        return None


def _unparsed(path: Path):
    """AHE-Beta's diagnostic of a persona reply it could not parse, written where the run keeps its user model's
    records (upstream writes it into the checkout, shared by every run)."""
    def dump(*, first: str, second: str) -> None:
        with contextlib.suppress(Exception):
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"first_raw": first[:4000], "retry_raw": second[:4000]}) + "\n")
    return dump


def _closing(control):
    """AHE-Beta's ConversationHandle.control, ending the request it interrupts. Its handle closes a request only on a
    reply, which an interrupt that does not wait never gets (the channel keeps that stop reply apart): the user's next
    message then joined the interrupted request, and the session's record (its request outcomes, which its audit and
    its deadline check read) merged the two."""
    if getattr(control, "ends_the_request", False):
        return control

    def interrupt(self, action):
        said = control(self, action)
        if self.in_flight:
            from ahe_beta.user.sim.runtime import utc_now
            self.in_flight = False
            self.last_request_status = "interrupted"
            self.request_outcomes.append({**(self._request or {}), "status": "interrupted", "finished_at": utc_now(),
                                          "termination_reason": "interrupted"})
            self._request = None
        return said
    interrupt.ends_the_request = True
    return interrupt


def _close() -> None:
    while _CLOSE:
        try:
            _CLOSE.pop()()
        except Exception:                                # noqa: BLE001 - the process is ending
            pass


def _public(issue):
    """An issue as a learner reads it: its body without AHE-Beta's metadata line (the persona's id and traits)."""
    if isinstance(issue, dict) and isinstance(issue.get("body"), str):
        return {**issue, "body": _cut(issue["body"])}
    return _cut(issue) if isinstance(issue, str) else issue


def _cut(text: str) -> str:
    cut, found = FOOTER.subn("", text)
    return cut.rstrip() if found else text


def visible(native: dict) -> dict:
    """AHE-Beta's verdict as user.py's contract has it: what a learner may read in the contract's keys, the rest in
    `files` (private)."""
    feedback = native.get("feedback") if isinstance(native.get("feedback"), dict) else {}
    if isinstance(feedback.get("opportunities"), list):
        feedback = {**feedback, "opportunities": [_public(issue) for issue in feedback["opportunities"]]}
    outcome = native.get("outcome") if isinstance(native.get("outcome"), dict) else {}
    experience = feedback.get("experience") if isinstance(feedback.get("experience"), dict) else {}
    said = {k: outcome[k] for k in ("end_reason", "status", "terminal_note", "review_verdict")
            if isinstance(outcome.get(k), str)}
    out = {"score": native.get("score"), "rating": native.get("rating"), "score_kind": native.get("score_kind"),
           "outcome": said, "reason": experience.get("reason") if isinstance(experience.get("reason"), str) else None,
           "issues": list(feedback.get("opportunities") or []), "feedback": feedback}
    if isinstance(native.get("usage"), dict):
        out["usage"] = native["usage"]
    files = dict(native.get("files") or {})
    files["outcome.json"] = json.dumps(native.get("outcome"), ensure_ascii=False, indent=1, default=str)
    return {**{k: v for k, v in out.items() if v is not None}, "files": files,
            "record": {k: native[k] for k in ("session_id", "artifact_root") if k in native}}


def simulate(task: dict, agent, workspace, params: dict) -> dict:
    global _MODEL
    with _LOCK:
        if _MODEL is None:
            _loopback_direct()                           # before the key probe and AHE-Beta's clients
            records = Path(params["artifact_root"]).parent / "user-model"
            records.mkdir(parents=True, exist_ok=True)
            _MODEL = _model_env(params.get("model"), records, bool(params.get("plan_admitted")))
            os.environ.update(_MODEL)
            os.environ["AHE_BETA_USAGE_PATH"] = str(records / "usage.jsonl")   # the run's, never an inherited one
            with contextlib.suppress(ImportError, AttributeError):   # its replies that failed to parse: the run's
                from ahe_beta.user.sim import persona_client         # records, not its checkout's backend/data
                persona_client._dump_unparseable = _unparsed(records / "parse_failures.jsonl")
            with contextlib.suppress(ImportError, AttributeError):   # an interrupt ends the request it stops
                from ahe_beta.integrations.hermit.conversation import ConversationHandle
                ConversationHandle.control = _closing(ConversationHandle.control)
            # each session's own observation store (its artifact_root), not one a native AHE-Beta run left in this
            # shell or its checkout's .env (which the persona client, imported above, loads)
            os.environ.pop("AHE_BETA_OBSERVATION_ROOT", None)
            atexit.register(_close)
    native = copy.deepcopy(dict(params))
    native.setdefault("run_id", "hermit")
    if params.get("binding"):                            # AHE-Beta's own Evolver: the commit the round's adapter is
        from binding import bound, entries               # (its feedback import requires it of each session)
        try:
            value = json.loads(Path(params["binding"]).read_text())
        except (OSError, ValueError) as exc:
            raise NotBound(f"the delivery binding cannot be read ({params['binding']}): {exc}") from exc
        commit = bound(value, adapter_sha256=params.get("adapter_sha256"), round_no=params.get("round"))
        if commit is None or (params.get("run_id") is not None and value.get("run_id") != params["run_id"]):
            raise NotBound(f"no delivery commit of this run, up to this session's round, holds its adapter "
                           f"({params['binding']}: rounds {[e.get('round') for e in entries(value)]} of "
                           f"{value.get('run_id')})")
        native["harness_sha"] = commit["harness_sha"]
    native.setdefault("harness_sha", params.get("adapter_sha256") or "no-adapter")
    scenario = ((native.get("scenario") or {}).get("workspace") or {}).get("params")
    if isinstance(scenario, dict):                       # an untitled issue's title: the task, never the persona
        scenario.setdefault("task_key", task.get("name") or task.get("id"))
    from ahe_beta.integrations.hermit.session import simulate as run   # the checkout on user.pythonpath
    try:
        verdict = visible(run(task, agent, workspace, native))
    except Exception as exc:                             # what it spent before failing still counts (its ledger)
        with contextlib.suppress(Exception):
            from ahe_beta.usage import default_usage_store
            exc.usage = spend(default_usage_store().events(session_id=native["session_id"]), _PRICE)
        if _SIGN_IN_LOST.is_set():                       # AHE-Beta's client retried it into a plain failure
            from hermit.agent.base.interface import LOGIN_FAILED
            raise SignInLost(f"the simulated user's model lost its sign-in ({type(exc).__name__}: {exc})"[:1500]
                             + f" [{LOGIN_FAILED}]") from exc
        raise
    usage = verdict.get("usage")
    if isinstance(usage, dict):                          # the session's tokens, priced as Hermit prices a call
        from ahe_beta.usage import default_usage_store   # from its calls, each as it reported (AHE-Beta's summary
        verdict["usage"] = {**usage, **spend(default_usage_store().events(session_id=native["session_id"]),
                                             _PRICE)}   # sums each field apart)
    return verdict


def spend(events: list, price: dict | None) -> dict:
    """AHE-Beta's usage events of one session, a call each (its prompt tokens count the cached ones), as user.py
    adds them to the rollout's: `tokens` (each call's known ones: the larger of its total and its parts, its cached
    ones a lower bound), `cost_usd` (of the calls that reported both parts; None without a price, a subscription say,
    or with no call counted) and `uncounted_calls` (calls that reported none, a part alone, or cached tokens past their
    prompt's, which then does not count them: the cost is a lower bound, and user.py takes the rollout's as unknown)."""
    def known(event, key):
        value = event.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else None
    tokens = counted = uncounted = 0
    cost = 0.0
    for event in (e for e in events if isinstance(e, dict)):
        given, said, total = known(event, "input_tokens"), known(event, "output_tokens"), known(event, "total_tokens")
        cached = known(event, "cached_input_tokens") or 0
        tokens += max(total or 0, (given or 0) + (said or 0), cached + (said or 0))
        if event.get("usage_source") == "unavailable" or given is None or said is None or cached > given:
            uncounted += 1
            continue
        counted += 1
        uncounted += (total or 0) > given + said        # a total its parts do not explain: priced short, said so
        if price:
            cost += ((given - cached) * price.get("input", 0) + said * price.get("output", 0)
                     + cached * price.get("cached_input", price.get("input", 0))) / 1e6
    return {"tokens": tokens, "cost_usd": cost if price and counted else None,
            **({"uncounted_calls": uncounted} if uncounted else {})}
