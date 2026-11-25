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
        return {"USER_LLM_BASE_URL": relay(bound.base_url), "USER_LLM_API_KEY": bound.key() or NO_KEY,
                "USER_LLM_MODEL": bound.model, "USER_LLM_REASONING_EFFORT": str(effort or "")}
    if os.environ.get("USER_LLM_BASE_URL") and os.environ.get("USER_LLM_MODEL"):
        return {}                                        # AHE-Beta's own variables, set by the caller
    raise Misconfigured("user.params.model names the user's model: a <backend>/<model> reference or {cli: ...}")


def _unparsed(path: Path):
    """AHE-Beta's diagnostic of a persona reply it could not parse, written where the run keeps its user model's
    records (upstream writes it into the checkout, shared by every run)."""
    def dump(*, first: str, second: str) -> None:
        with contextlib.suppress(Exception):
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"first_raw": first[:4000], "retry_raw": second[:4000]}) + "\n")
    return dump


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
            records = Path(params["artifact_root"]).parent / "user-model"
            records.mkdir(parents=True, exist_ok=True)
            _MODEL = _model_env(params.get("model"), records, bool(params.get("plan_admitted")))
            os.environ.update(_MODEL)
            os.environ["AHE_BETA_USAGE_PATH"] = str(records / "usage.jsonl")   # the run's, never an inherited one
            with contextlib.suppress(ImportError, AttributeError):   # its replies that failed to parse: the run's
                from ahe_beta.user.sim import persona_client         # records, not its checkout's backend/data
                persona_client._dump_unparseable = _unparsed(records / "parse_failures.jsonl")
            atexit.register(_close)
    native = copy.deepcopy(dict(params))
    native.setdefault("run_id", "hermit")
    if params.get("binding"):                            # AHE-Beta's own Evolver: the commit the round's adapter is
        try:                                             # (its feedback import requires it of each session)
            bound = json.loads(Path(params["binding"]).read_text())
        except (OSError, ValueError) as exc:
            raise NotBound(f"the delivery binding cannot be read ({params['binding']}): {exc}") from exc
        if bound.get("adapter_sha256") != params.get("adapter_sha256") or (
                params.get("round") is not None and bound.get("round") != int(params["round"])) or (
                params.get("run_id") is not None and bound.get("run_id") != params["run_id"]):
            raise NotBound(f"this session's adapter, round and run are not the ones bound to the delivery commit "
                           f"({params['binding']}: round {bound.get('round')} of {bound.get('run_id')})")
        native["harness_sha"] = bound["harness_sha"]
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
            exc.usage = spend(default_usage_store().summary(session_id=native["session_id"]), _PRICE)
        if _SIGN_IN_LOST.is_set():                       # AHE-Beta's client retried it into a plain failure
            from hermit.agent.base.interface import LOGIN_FAILED
            raise SignInLost(f"the simulated user's model lost its sign-in ({type(exc).__name__}: {exc})"[:1500]
                             + f" [{LOGIN_FAILED}]") from exc
        raise
    usage = verdict.get("usage")
    if isinstance(usage, dict):                          # the session's tokens, priced as Hermit prices a call
        verdict["usage"] = {**usage, **spend(usage, _PRICE)}
    return verdict


def spend(usage: dict, price: dict | None) -> dict:
    """AHE-Beta's usage of one session (its prompt tokens count the cached ones) as user.py adds it to the rollout's:
    `tokens`, `cost_usd` (of the calls it counted; None without a price, a subscription say) and `uncounted_calls`
    (calls whose usage went unreported: the cost is then a lower bound)."""
    number = lambda key: int(usage.get(key) or 0)   # noqa: E731
    tokens = number("total_tokens") or number("input_tokens") + number("output_tokens")
    counted = number("input_tokens") + number("output_tokens")   # what a price applies to: none counted (a total
    cost = None                                     # alone, calls unreported, a ledger missing, which sums to
    if price and counted:                           # zeros) is unknown, not 0
        cost = ((number("input_tokens") - number("cached_input_tokens")) * price.get("input", 0)
                + number("cached_input_tokens") * price.get("cached_input", price.get("input", 0))
                + number("output_tokens") * price.get("output", 0)) / 1e6
    uncounted = number("unavailable_count") or (1 if number("total_tokens") > counted or not counted else 0)
    return {"tokens": tokens, "cost_usd": cost, **({"uncounted_calls": uncounted} if uncounted else {})}
