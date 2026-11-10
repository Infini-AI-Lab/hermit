"""One mini-swe-agent task, run as the `mini-swe-agent` Base Agent's process (hermit.agent.base.mini_swe_agent).

    python -m hermit.agent.base.mini_swe_agent_run --settings <home>/mini-swe-agent.json --cwd <workspace> \
        --last <home>/last_message.txt [--system <instructions> | --system-file <file>] --stdin < prompt

The task runs as mini-swe-agent's batch runner runs one: a model, an environment and a DefaultAgent from its Python
API, the config Hermit's prompts over mini-swe-agent's own (hermit.agent.base.mini_swe_agent.render_config) and the
settings' `config` over them. Every message is written to stdout as one JSON event as it is added; the reply (the
submission, else the last assistant message) goes to `--last`. Events:

    {"type": "session.started", "session": <id>, "model": <litellm model>}
    {"type": "message", "message": {"role", "content", "actions", "returncode", "output", "exit_status",
                                    "submission", ...}, "usage": {"input_tokens", "cached_input_tokens",
                                    "output_tokens"}}        # usage: on a model response
    {"type": "error", "message": "<exception>: <text>", "unavailable": true}   # then the process exits 1;
                                     # `unavailable`: the provider could not serve (after mini-swe-agent's retries)

A setting that fails before the session (a config that names the model or the environment class), or a first model
call that fails for a setting (a wrong key, a model that does not exist), is reported on stderr, with no event, and
the process exits 2: the Base Agent's caller sees a session that never started, not a wrong answer. Until the model
has answered, the events are held back for that reason (Stream). At SIGTERM (the Base Agent's timeout) the running
commands are killed too. On Linux the process makes itself non-dumpable, so the commands it runs (the same user)
cannot read its environment, where the model's key is.

The model's key is read from the binding's variable and passed with each request; it is in no event, and the
commands' environment holds `env` (the variables `env_names` names) and a few basic ones only.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import sys
from typing import Any, Callable
import uuid

import yaml

from hermit.agent.base.mini_swe_agent import (CONFIG_FILE, STEP_LIMIT, adapter_block, base_config,
                                              load_minisweagent, render_config, tools_section, with_placeholder)
from hermit.agent.model.backends import PLACEHOLDER_KEY

CALL_TIMEOUT_S = 600             # one model request, at most; the session's own clock is the Base Agent's


def event_of(message: dict) -> dict:
    """A mini-swe-agent message as a stream event: its chat fields, the commands it asked for, an observation's
    output and exit code, an exit's status and submission, and a response's token counts; not its raw response."""
    extra = message.get("extra") or {}
    content = message.get("content")
    if isinstance(content, list):                    # a multimodal message: its text parts
        content = "\n".join(str(p.get("text") or "") for p in content if isinstance(p, dict))
    out: dict[str, Any] = {"role": message.get("role"), "content": "" if content is None else str(content)}
    for key in ("tool_calls", "tool_call_id"):
        if message.get(key) is not None:
            out[key] = message[key]
    if message.get("reasoning_content"):             # a thinking model's (litellm's field)
        out["reasoning"] = str(message["reasoning_content"])
    if extra.get("actions"):
        out["actions"] = [str(a.get("command", "")) for a in extra["actions"] if isinstance(a, dict)]
    if "returncode" in extra:
        out["returncode"] = extra["returncode"]
        out["output"] = str(extra.get("raw_output") or "")
    for key in ("exit_status", "submission"):
        if key in extra:
            out[key] = extra[key]
    event: dict[str, Any] = {"type": "message", "message": out}
    response = extra.get("response")
    usage = (response.get("usage") if isinstance(response, dict) else None) or {}
    if usage:
        event["usage"] = {"input_tokens": int(usage.get("prompt_tokens") or 0),
                          "cached_input_tokens": int((usage.get("prompt_tokens_details") or {}).get("cached_tokens")
                                                     or 0),
                          "output_tokens": int(usage.get("completion_tokens") or 0)}
    return event


def _emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def bound_model(model_class: type, binding: dict | None) -> type:
    """`model_class` calling the binding's endpoint with its key, both passed with each request (the model's config,
    which mini-swe-agent writes into its trajectory, holds neither), each request bounded by CALL_TIMEOUT_S."""
    settings: dict[str, Any] = {"timeout": CALL_TIMEOUT_S}
    if binding:
        settings["api_base"] = binding["base_url"]
        settings["api_key"] = os.environ.get(binding.get("api_key_env") or "", "") or PLACEHOLDER_KEY

    import litellm

    class Bound(model_class):
        # a 400 is the request's: sent again it fails again (a wrong key on DeepSeek is one), so it is not retried
        abort_exceptions = [*getattr(model_class, "abort_exceptions", []), litellm.exceptions.BadRequestError]

        def _query(self, messages, **kwargs):
            return super()._query(messages, **{**settings, **kwargs})

    Bound.__name__, Bound.__qualname__, Bound.__module__ = (model_class.__name__, model_class.__qualname__,
                                                            model_class.__module__)
    return Bound


def build(settings: dict, cwd: str, emit: Callable[[dict], None]):
    """(the agent, the adapter's block) for one task."""
    load_minisweagent()
    import litellm
    from minisweagent.agents.default import DefaultAgent
    from minisweagent.models import get_model_class
    from hermit.agent.base.mini_swe_agent_env import HermitEnvironment

    litellm.suppress_debug_info = True            # its help text would go to stdout, between the events
    model_class = str(settings.get("model_class") or "litellm")
    user = settings.get("config") or {}               # the config file's settings, read by the Base Agent
    given = [f"model.{k}" for k in ("model_name", "model_class") if k in (user.get("model") or {})]
    given += ["environment.environment_class"] if "environment_class" in (user.get("environment") or {}) else []
    if given:
        raise ValueError(f"mini-swe-agent's config sets {' and '.join(given)}: the model is the Base Agent's model "
                         "and model_class, and the environment is Hermit's local one")
    config = render_config(base_config(model_class), user)

    deployed = Path(cwd) / CONFIG_FILE                 # the workspace's deployment, as `mini -c` would read it
    context = ""
    if deployed.is_file() and not deployed.is_symlink():
        context = str(((yaml.safe_load(deployed.read_text()) or {}).get("hermit") or {}).get("context") or "")
    env_config = {k: v for k, v in (config.get("environment") or {}).items() if k != "environment_class"}
    extra_env = {n: os.environ[n] for n in settings.get("env_names") or [] if n in os.environ}
    env = HermitEnvironment(extra_env=extra_env, **{**env_config, "cwd": cwd, "inherit_env": False})
    block = adapter_block(context, tools_section(env.tool_list()), str(settings.get("system") or ""))

    binding = settings.get("binding")
    model_config = dict(config.get("model") or {})
    model_kwargs = dict(model_config.pop("model_kwargs", None) or {})
    if binding and binding.get("extra_body"):
        model_kwargs["extra_body"] = {**binding["extra_body"], **(model_kwargs.get("extra_body") or {})}
    if settings.get("effort"):
        model_kwargs.setdefault("reasoning_effort", settings["effort"])
    name = binding["litellm"] if binding else settings.get("model")
    if not name:
        raise ValueError("mini-swe-agent needs a model: a <backend>/<model> reference or a litellm model id")
    model = bound_model(get_model_class(name, model_class), binding)(
        **{**model_config, "model_name": name, "model_kwargs": model_kwargs, "cost_tracking": "ignore_errors"})

    class Streaming(DefaultAgent):
        def add_messages(self, *messages: dict) -> list[dict]:
            added = super().add_messages(*messages)
            for message in messages:
                emit(event_of(message))
            return added

    agent_config = {k: v for k, v in (config.get("agent") or {}).items() if k != "agent_class"}
    agent_config["system_template"] = with_placeholder(agent_config["system_template"])
    step_limit = (user.get("agent") or {}).get("step_limit")      # 0 is mini-swe-agent's "no limit"
    step_limit = settings.get("step_limit") if step_limit is None else step_limit
    agent_config["step_limit"] = int(STEP_LIMIT if step_limit is None else step_limit)
    agent_config["cost_limit"] = 0                # the cost is Hermit's to count (cost tracking is off)
    agent_config["output_path"] = None
    return Streaming(model, env, **agent_config), block


def reply(messages: list[dict]) -> str:
    """The submission (the output of the command that submitted it, its end of line left out), else the last
    assistant message."""
    if messages and messages[-1].get("role") == "exit" and (messages[-1].get("extra") or {}).get("submission"):
        return str(messages[-1]["extra"]["submission"]).strip()
    for message in reversed(messages):
        content = message.get("content")
        if message.get("role") == "assistant" and isinstance(content, str) and content.strip():
            return content
    return ""


def _setup_error(exc: Exception) -> bool:
    """Whether the first model call failed for a setting every task shares: the key, the model id, a parameter. The
    first request holds only the system message and the task, so a 400 then is the settings' (some providers answer
    a wrong key with one), unless it is the task's size or content."""
    import litellm
    e = litellm.exceptions
    if isinstance(exc, (e.ContextWindowExceededError, e.ContentPolicyViolationError)):
        return False
    return isinstance(exc, (e.AuthenticationError, e.PermissionDeniedError, e.NotFoundError, e.BadRequestError))


class Stream:
    """The events on stdout. Until the model has answered once they are held back: a call that fails then for a
    setting every task shares (a wrong key, a model that does not exist) is reported as a session that never started
    (stderr, no event), not as a task without an answer."""

    def __init__(self, started: dict):
        self.started, self.held = started, []

    @property
    def answered(self) -> bool:
        return self.held is None

    def __call__(self, event: dict) -> None:
        if self.held is None:
            _emit(event)
            return
        self.held.append(event)
        message = event.get("message") or {}
        if message.get("role") == "assistant" or event.get("usage"):      # a response, a format error's included
            self.flush()

    def flush(self) -> None:
        if self.held is not None:
            for event in [self.started, *self.held]:
                _emit(event)
            self.held = None


def _stop(signum, frame) -> None:
    """The Base Agent stops this process at its timeout: the commands running in sessions of their own go too."""
    from hermit.agent.base.mini_swe_agent_env import stop_commands
    stop_commands()
    os._exit(128 + signum)


def _unavailable(exc: Exception) -> bool:
    """Whether the provider could not serve (a rate limit, an outage, no connection), not the request was wrong."""
    import litellm
    e = litellm.exceptions
    return isinstance(exc, (e.RateLimitError, e.ServiceUnavailableError, e.APIConnectionError, e.InternalServerError,
                            e.BadGatewayError, e.Timeout))


def _not_dumpable() -> None:
    """On Linux, this process's memory and /proc/<pid>/environ out of reach of the commands it runs (the same
    user's processes), as Codex keeps its own: the model's key is in this process's environment."""
    if sys.platform.startswith("linux"):
        import ctypes
        ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0)        # PR_SET_DUMPABLE, 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--settings", type=Path, required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--last", type=Path, required=True)
    parser.add_argument("--prompt")
    parser.add_argument("--stdin", action="store_true")
    parser.add_argument("--system", default="")      # the call's system instructions (a sandbox translates its paths)
    parser.add_argument("--system-file", type=Path)  # or, past what one argument holds, the file they are in
    args = parser.parse_args(argv)
    _not_dumpable()
    signal.signal(signal.SIGTERM, _stop)
    prompt = sys.stdin.read() if args.stdin else (args.prompt or "")
    system = args.system_file.read_text(encoding="utf-8") if args.system_file else args.system
    settings = {**json.loads(args.settings.read_text()), "system": system}
    args.last.write_text("", encoding="utf-8")
    stream = Stream({"type": "session.started", "session": uuid.uuid4().hex,
                     "model": (settings.get("binding") or {}).get("litellm") or settings.get("model")})
    try:
        agent, block = build(settings, args.cwd, stream)
    except Exception as exc:          # a setting that fails every task: no session starts, nothing is an event
        print(f"hermit mini-swe-agent: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 2
    try:
        agent.run(prompt, adapter_context=block)
    except Exception as exc:          # the run's exit message is in the stream (DefaultAgent adds it)
        if not stream.answered and _setup_error(exc):
            print(f"hermit mini-swe-agent: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            return 2
        stream.flush()
        event = {"type": "error", "message": f"{type(exc).__name__}: {exc}"}
        if _unavailable(exc):
            event["unavailable"] = True
        _emit(event)
        return 1                      # no answer: the last message of a run that broke off is none
    stream.flush()
    args.last.write_text(reply(agent.messages), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
