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

The user's model is Layer 1 (docs/framework/agent-layers.md): a `<backend>/<model>` reference is the backend's
OpenAI-compatible URL and key; `{cli: ...}` is a chat endpoint over that CLI (hermit.agent.base.endpoint), each request
one session in a bubblewrap sandbox of its own that reaches only the login's hosts (hermit.isolate.cli_sandbox), as an
acting pipeline's `model: {cli: ...}` runs. AHE-Beta's client reads its model from USER_LLM_BASE_URL, USER_LLM_API_KEY
and USER_LLM_MODEL; they are set here, once for the process (every session of a run has the same user model).

AHE-Beta's provenance (`run_id`, `harness_sha`) comes from the run: `harness_sha` is the adapter's content digest that
user.py hands over (`adapter_sha256`), unless the assignment names a delivery repository's commit (../delivery/).
"""
from __future__ import annotations

import atexit
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
        _CLOSE.append(endpoint.close)
        if proxy is not None:
            _CLOSE.append(proxy.close)
        env = {"USER_LLM_BASE_URL": endpoint.url, "USER_LLM_API_KEY": "hermit-local-endpoint",
               "USER_LLM_MODEL": str(settings.get("model") or settings["cli"])}
        if settings.get("effort"):
            env["USER_LLM_REASONING_EFFORT"] = str(settings["effort"])
        return env
    if isinstance(spec, str) and spec:
        from hermit.agent.model.backends import as_binding, registry
        bound = as_binding(spec, registry())
        if bound is None:
            raise ValueError(f"user.params.model {spec!r} is neither a <backend>/<model> reference nor {{cli: ...}}")
        return {"USER_LLM_BASE_URL": bound.base_url, "USER_LLM_API_KEY": bound.backend.key() or "",
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
    return run(task, agent, workspace, native)
