"""The `mini-swe-agent` agent: one task run by mini-swe-agent (https://mini-swe-agent.com) in a fresh workspace.

    agent:
      name: mini-swe-agent
      params:
        model: deepseek/deepseek-flash         # <backend>/<model id> (hermit.backends); the run's `model` is the
                                             # default. mini-swe-agent calls it through litellm at the backend's URL
        model_class: litellm                 # mini-swe-agent's model class: litellm (its bash tool, the default) or
                                             # litellm_textbased (one ```mswea_bash_command block per response)
        config: agent/mini.yaml              # optional: a mini-swe-agent config merged over Hermit's (its agent,
                                             # model and environment sections, e.g. agent.step_limit: 100)
        user: "{question}"                   # the task text, from the task's fields (default: the task's prompt,
                                             # question, instruction or input field)
        files: [environment/fixtures]        # optional: copied into every workspace before the run
        env: {}                              # variables for the agent's commands (a `$NAME` value is expanded)
        timeout_s: 3600                      # wall time per task, checked before each model call
        workspaces: .logs/workspaces         # one fresh directory per task, beside the config

mini-swe-agent runs in this process (its Python API), one model, environment and agent per task, the way its own
batch runner runs. The messages are built as `hermit deploy --host mini-swe-agent` writes them
(hermit.hosts.mini_swe_agent): Hermit's prompts over mini-swe-agent's own config, with the adapter's context in the
system message, followed by what its `context_tool` renders for this task when the manifest declares one (recorded in
the output's `extra["context_tool"]`). The adapter's tools are the command `hermit-tool <name> '<JSON>'`, answered
through a Session; its skills are copied to the workspace's `.hermit/skills/`. Roles are not used: mini-swe-agent has
no subagents. The commands run in the workspace and see `env` and a few basic variables (PATH, HOME, LANG, ...), not
this process's environment, where the model's key is; the key reaches litellm with each call and is never written to
the trajectory, nor are the expanded `env` values. That is not confinement: a command runs as this user, and only
the run's sandbox (`sandbox.enabled`, Linux with bubblewrap) confines it. Another environment class in `config`
(docker, ...) is passed to mini-swe-agent for a run with no adapter tools, skills or `files`, which reach the local
workspace only.

The agent submits its answer as mini-swe-agent's commands end a task (`echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT &&
cat answer.txt`); the output is that submission, or the last assistant message when there is none (a step limit, a
timeout). The workspace is the log: the trajectory (`trajectory.traj.json`) and whatever the agent wrote. Tokens are
summed from the responses, and the cost is computed at the binding's price (unknown without one); mini-swe-agent's
own cost tracking, which needs litellm to know the model, is off, so `agent.step_limit` (50) and `timeout_s` stop a
task: the wall time is checked before each model call, one request is bounded by 600 s (or `timeout_s`), and a
command by the environment's `timeout`. A wrong key, model or setting raises; any other error (a context overflow,
retries exhausted) ends the task with what it did so far, its name in `extra["exit_status"]`.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import time
import uuid

import yaml

from hermit.adapter.session import Session
from hermit.agents.base import Agent
from hermit.agents.react import user_message
from hermit.backends import PLACEHOLDER_KEY, Binding
from hermit.evolver.agent import price_usage
from hermit.evolver.base import Usage
from hermit.hosts import mini_swe_agent as host
from hermit.task import RunOutput

TRAJECTORY = "trajectory.traj.json"
STEP_LIMIT = 50                  # model calls per task, unless the config's agent.step_limit says otherwise
CALL_TIMEOUT_S = 600             # one model request, at most (timeout_s when shorter)
LOCAL = ("local", "minisweagent.environments.local.LocalEnvironment", host.ENVIRONMENT_CLASS)
CHAT_KEYS = ("role", "content", "tool_calls", "tool_call_id")


class MiniSweAgent(Agent):
    def run(self, task: dict, adapter: Path | None) -> RunOutput:
        return _run(task, adapter, self.params, self.base)


def run(task: dict, adapter: Path | None, *, params: dict, base: Path) -> RunOutput:
    """The function form; MiniSweAgent is the class (`agent.name: mini-swe-agent`)."""
    return _run(task, adapter, params, base)


def bound_model(model_class: type, binding: Binding, timeout_s: float) -> type:
    """`model_class` calling the binding's endpoint with its key, both passed with each call (the model's config,
    which mini-swe-agent writes into the trajectory, holds neither), each request bounded by `timeout_s`."""
    settings = {"api_base": binding.base_url, "api_key": binding.key() or PLACEHOLDER_KEY, "timeout": timeout_s}

    class Bound(model_class):
        def _query(self, messages, **kwargs):
            return super()._query(messages, **{**settings, **kwargs})

    Bound.__name__, Bound.__qualname__, Bound.__module__ = model_class.__name__, model_class.__qualname__, model_class.__module__
    return Bound


def usage_of(messages: list[dict], model: str, calls: int) -> Usage:
    """The tokens of every response in the trajectory (a format error's response included: it was billed)."""
    usage = Usage(model=model, calls=calls)
    for message in messages:
        response = (message.get("extra") or {}).get("response")
        u = (response.get("usage") if isinstance(response, dict) else None) or {}
        usage.input_tokens += int(u.get("prompt_tokens") or 0)
        usage.cached_input_tokens += int((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        usage.output_tokens += int(u.get("completion_tokens") or 0)
    return usage


def chat_messages(messages: list[dict]) -> list[dict]:
    """The trajectory as chat-completions messages, for the Evolvers that read them: mini-swe-agent's raw responses
    (`extra`) and its closing `exit` message left out."""
    out = []
    for message in messages:
        if message.get("role") not in ("system", "user", "assistant", "tool"):
            continue
        out.append({k: message[k] for k in CHAT_KEYS if message.get(k) is not None})
    return out


def _last_assistant(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message.get("role") == "assistant" and isinstance(message.get("content"), str) and message["content"].strip():
            return message["content"]
    return ""


def _run(task: dict, adapter: Path | None, params: dict, base: Path) -> RunOutput:
    spec = params.get("model")
    if not isinstance(spec, dict) or not spec.get("base_url") or not spec.get("model"):
        raise ValueError("agent.params.model is required: a `<backend>/<model>` reference, or {base_url, model, "
                         "api_key_env, price} (the run's `model` is the default)")
    binding = Binding.from_spec(spec)
    if binding.backend.key_required and binding.api_key_env and not binding.key():
        raise RuntimeError(f"{binding.api_key_env} is not set (the key of backend {binding.backend.name!r})")
    host.load_minisweagent()
    from minisweagent.agents.default import DefaultAgent
    from minisweagent.environments import get_environment
    from minisweagent.models import get_model_class
    import litellm
    from hermit.hosts.mini_swe_agent_env import HermitEnvironment
    # A wrong key, model or setting fails every task alike: raised, as a failed run. Any other error ends this task
    # only (a context overflow, retries exhausted) and is returned with the calls made so far.
    fatal = (litellm.exceptions.AuthenticationError, litellm.exceptions.PermissionDeniedError,
             litellm.exceptions.NotFoundError, litellm.exceptions.UnsupportedParamsError)

    model_class = str(params.get("model_class") or "litellm")
    user_config = yaml.safe_load(Path(base, params["config"]).read_text()) if params.get("config") else None
    set_here = [f"model.{k}" for k in ("model_name", "model_class") if k in ((user_config or {}).get("model") or {})]
    if set_here:
        raise ValueError(f"agent mini-swe-agent: set the model with agent.params.model and model_class, not "
                         f"{' and '.join(set_here)} in {params['config']}")
    config = host.render_config(host.base_config(model_class), user_config or {})
    workspace = Path(base) / params.get("workspaces", ".logs/workspaces") / f"{task.get('id', 'task')}-{uuid.uuid4().hex[:8]}"
    workspace.mkdir(parents=True)
    for item in params.get("files") or []:
        source = Path(base) / item
        target = workspace / source.name
        shutil.copytree(source, target) if source.is_dir() else shutil.copy2(source, target)
    text = user_message(task, params.get("user"))
    session = Session(adapter) if adapter is not None else None
    try:
        # The adapter's context for this task, its tools and its skills, as a deployed workspace has them.
        context = session.context(query=text, task=task) if session else ""
        tools = session.tools if session else []
        skills = []
        for directory in session.skills() if session else []:
            shutil.copytree(directory, workspace / host.SKILLS_DIR / directory.name, symlinks=True)
            skills.append(directory.name)
        block = host.adapter_block(context, tools, host.skill_index(workspace / host.SKILLS_DIR, skills))

        env_config = dict(config.get("environment") or {})
        env_class = env_config.pop("environment_class", "local")
        commands_env = {k: os.path.expandvars(str(v)) for k, v in (params.get("env") or {}).items()}
        if env_class in LOCAL:
            env = HermitEnvironment(session=session, extra_env=commands_env,
                                    **{**env_config, "cwd": str(workspace), "inherit_env": False})
        else:
            needs = [what for what, given in (("tools", tools), ("skills", skills), ("files", params.get("files")))
                     if given]
            if needs:
                raise ValueError(f"agent mini-swe-agent: the adapter's {' and '.join(needs)} reach the local "
                                 f"environment's workspace only, and the config names {env_class!r}")
            env = get_environment({**env_config, "environment_class": env_class})

        timeout = int(params.get("timeout_s", 3600))
        model_config = dict(config.get("model") or {})
        model_kwargs = dict(model_config.pop("model_kwargs", None) or {})
        if binding.extra_body:
            model_kwargs["extra_body"] = {**binding.extra_body, **(model_kwargs.get("extra_body") or {})}
        model = bound_model(get_model_class(binding.litellm_model, model_class), binding, min(timeout, CALL_TIMEOUT_S))(
            **{**model_config, "model_name": binding.litellm_model, "model_kwargs": model_kwargs,
               "cost_tracking": "ignore_errors"})

        agent_config = {k: v for k, v in (config.get("agent") or {}).items() if k != "agent_class"}
        agent_config["system_template"] = host.with_placeholder(agent_config["system_template"])
        agent_config["step_limit"] = int(((user_config or {}).get("agent") or {}).get("step_limit", STEP_LIMIT))
        agent_config["cost_limit"] = 0                       # the cost is Hermit's to count (cost tracking is off)
        agent_config["wall_time_limit_seconds"] = timeout
        agent_config["output_path"] = workspace / TRAJECTORY
        agent = DefaultAgent(model, env, **agent_config)
        started, error = time.time(), None
        try:
            result = agent.run(text, adapter_context=block)
        except fatal:
            raise
        except Exception as exc:                 # recorded in the trajectory; the calls made so far still count
            error = exc
            result = {"exit_status": type(exc).__name__, "submission": ""}
        wall = round(time.time() - started, 1)
    finally:
        if session is not None:
            session.close()

    usage = usage_of(agent.messages, binding.model, agent.n_calls)
    price_usage([usage], {"price": spec.get("price")})
    submission = (result.get("submission") or "").strip()
    extra = {"exit_status": result.get("exit_status"), "workspace": str(workspace), "trajectory": str(workspace / TRAJECTORY)}
    if error is not None:
        extra["error"] = f"{type(error).__name__}: {error}"
    if session is not None and session.last_context is not None:
        extra["context_tool"] = session.last_context          # what the adapter's context tool rendered for this task
    return RunOutput(output=submission or _last_assistant(agent.messages), log=workspace, cost_usd=usage.cost_usd,
                     wall_s=wall, api_speed={"model_calls": agent.n_calls}, messages=chat_messages(agent.messages),
                     tokens=usage.input_tokens + usage.output_tokens, extra=extra)
