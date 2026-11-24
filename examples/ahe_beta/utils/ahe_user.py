"""AHE-Beta's own simulated user (its checkout's `ahe_beta.integrations.hermit.session`) behind user.py's simulate
contract, its model reached through Hermit.

    user:
      simulate: ahe_user:simulate
      pythonpath: [$AHE_ROOT/backend]          # the AHE-Beta checkout (pinned in ../README.md)
      assignments: ../cohort/assignments.json  # each row's persona, scenario and round (prepare.py), or task_field
      params:
        run_id: u8-codex                       # the session's provenance in AHE-Beta's records
        model: {cli: codex, model: gpt-5.6-luna, login: chatgpt, effort: low}   # or a <backend>/<model> reference
        unapplied: [temperature]               # what AHE-Beta's client asks that a CLI session does not apply

The user's model is Layer 1 (docs/framework/agent-layers.md): a `<backend>/<model>` reference is reached through a
local relay endpoint (one Direct call a request: hermit.agent.base.endpoint.relay), the backend's key passed on;
`{cli: ...}` is a chat endpoint over that CLI, each request one session in a bubblewrap sandbox of its own that reaches
only the login's hosts (hermit.isolate.cli_sandbox), as an acting pipeline's `model: {cli: ...}` runs. On a login those
sessions count as the rollout's: the run already admitted the task's environment to the login's plan (its wait and its
slot, hermit.isolate), so a session of the user's neither waits for the plan nor takes a slot of its own, which with
SLOTS at or under the agent's concurrency would wait for a slot only a finished rollout frees. AHE-Beta's client reads
its model from USER_LLM_BASE_URL, USER_LLM_API_KEY and USER_LLM_MODEL; they are set here, once for the process (every
session of a run has the same user model).

What a learner reads of AHE-Beta's verdict (user.py's VISIBLE) is mapped here: the rating, its structured `feedback`
(the task assessment, the experience and its reason, the attribution, the opportunities it raises as `issues`) and the
outcome's end reason and terminal note. Its full `user_outcome`, which in a session without interaction holds the
review's call traces (their system prompt is the persona), stays private (files/outcome.json), as does its session
record.

AHE-Beta's provenance (`run_id`, `harness_sha`) comes from the run: `harness_sha` is the adapter's content digest that
user.py hands over (`adapter_sha256`), unless the assignment names a delivery repository's commit (../delivery/).
"""
from __future__ import annotations

import atexit
import json
import os
from pathlib import Path
import threading

_LOCK = threading.Lock()
_MODEL: dict | None = None           # the user model's variables, once set
_CLOSE: list = []


def _model_env(spec, records: Path) -> dict:
    """USER_LLM_* for AHE-Beta's client: a <backend>/<model> reference's URL, key and model id, or a chat endpoint over
    a CLI (`{cli: ...}`), started once for the process and closed at its exit."""
    if isinstance(spec, dict) and spec.get("cli"):
        from hermit.agent.base.endpoint import cli_endpoint
        from hermit.isolate import cli_sandbox
        settings = {"web_search": False, **spec}
        if settings.get("login") and not settings.get("model"):
            raise ValueError("user.params.model: a login needs the CLI's model name")
        records.mkdir(parents=True, exist_ok=True)
        box, proxy = cli_sandbox(settings, records=records, role="user-model")
        endpoint = cli_endpoint(settings, records / "sessions", sandbox=box)
        place = endpoint.place

        def admitted(work):                              # the rollout holds the login's plan admission (docstring)
            environment = place(work)
            environment.plan_admitted = True
            return environment
        endpoint.place = admitted
        _CLOSE.append(endpoint.close)
        if proxy is not None:
            _CLOSE.append(proxy.close)
        env = {"USER_LLM_BASE_URL": endpoint.url, "USER_LLM_API_KEY": "hermit-local-endpoint",
               "USER_LLM_MODEL": str(settings.get("model") or settings["cli"])}
        if settings.get("effort"):
            env["USER_LLM_REASONING_EFFORT"] = str(settings["effort"])
        return env
    if isinstance(spec, str) and spec:
        from hermit.agent.base.endpoint import relay
        from hermit.agent.model.backends import as_binding, registry
        bound = as_binding(spec, registry())
        if bound is None:
            raise ValueError(f"user.params.model {spec!r} is neither a <backend>/<model> reference nor {{cli: ...}}")
        return {"USER_LLM_BASE_URL": relay(bound.base_url), "USER_LLM_API_KEY": bound.backend.key() or "",
                "USER_LLM_MODEL": bound.model}
    if os.environ.get("USER_LLM_BASE_URL") and os.environ.get("USER_LLM_MODEL"):
        return {}                                        # AHE-Beta's own variables, set by the caller
    raise ValueError("user.params.model names the user's model: a <backend>/<model> reference or {cli: ...}")


def _close() -> None:
    while _CLOSE:
        try:
            _CLOSE.pop()()
        except Exception:                                # noqa: BLE001 - the process is ending
            pass


def visible(native: dict) -> dict:
    """AHE-Beta's verdict as user.py's contract has it: what a learner may read in the contract's keys, the rest in
    `files` (private)."""
    feedback = native.get("feedback") if isinstance(native.get("feedback"), dict) else {}
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
            _MODEL = _model_env(params.get("model"), records)
            os.environ.update(_MODEL)
            atexit.register(_close)
    native = dict(params)
    native.setdefault("run_id", "hermit")
    native.setdefault("harness_sha", params.get("adapter_sha256") or "no-adapter")
    from ahe_beta.integrations.hermit.session import simulate as run   # the checkout on user.pythonpath
    return visible(run(task, agent, workspace, native))
