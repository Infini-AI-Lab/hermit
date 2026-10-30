"""OfficeQA as in the SkillOpt paper, run by SkillOpt's own rollout (skillopt.envs.officeqa.rollout.process_one), in
one of the paper's two harnesses for it, `agent.params.harness`:

direct (the default), its offline mode: the question's oracle parsed pages in the prompt, glob/read/grep tools limited
to the question's source files, at most 24 turns of up to 16,384 tokens, the answer inside <answer>...</answer>.
codex, its Codex harness: `codex exec` in a workspace-write sandbox without network or web search, in a fresh
workspace with the skill as .agents/skills/skillopt-target/SKILL.md, the question with its oracle pages as task.md and
the whole corpus under docs/ (the question's source files as hints). A session has 180 s; one without an <answer> is
run again with its reply, up to 24 sessions. SkillOpt summarizes the sessions in codex_trace_summary.txt, which its
analyst reads. Codex runs through codex_sandbox.py, in a container of its own (docker/) that sees only the workspace
and the corpus and reaches only the model provider, with Codex's workspace-write sandbox inside it.

The adapter's context is the skill: ADAPTER.md and, below it, what the adapter's context tool renders for the question
when its manifest declares one (ReHarness's retrieved reflections; Session.context(query=question, task=row), whose
whole result is recorded in the output's extra["context_tool"]). adapter=None runs without one. The run's SkillOpt
prediction files (conversation.json, target_*_prompt.txt; for codex also codex_raw.txt and codex_trace_summary.txt)
are its log.

The target model (`agent.params.model`, a `<backend>/<model>` reference, see hermit.backends; default GLM 5.3
Flash on Fireworks, FIREWORKS_API_KEY) answers at medium reasoning effort, as the paper runs its student: SkillOpt's
chat_target_messages calls it directly, Codex through the backend's Responses API with the provider settings in a
CODEX_HOME of its own (no user configuration or instructions). Codex's workspaces are under `agent.params.codex_dir`,
outside any checkout: Codex reads the instructions of the repository it runs in. A rollout the API could not serve
raises, so it is recorded as an error rather than a wrong answer (so does a question whose parsed pages SkillOpt cannot find under data.json's docs: without them the rollout is not the paper's setting); a Codex session past its 180 s is a failed answer,
as in SkillOpt.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time

from hermit.agents import Agent
from hermit.task import RunOutput

HERE = Path(__file__).resolve().parent
DATA = json.loads((HERE.parent / "data.json").read_text())
DEFAULT_MODEL = "fireworks/accounts/fireworks/models/glm-5p3-flash"
EFFORT = "medium"                                                 # paper p.8: teacher and student at medium
MAX_TURNS, MAX_TOKENS = 24, 16384                                 # configs/officeqa/default.yaml; issue #13
UNAVAILABLE = "target API unavailable"
CODEX_DIR = "~/.cache/hermit/officeqa-codex-actor"         # the codex harness's homes and rollouts/
# Codex's own reports of a provider it could not reach (it retries those itself first), and of a container that could
# not start (codex_sandbox.py), not the session's text
CODEX_UNAVAILABLE = re.compile(r"unexpected status (?:429|5\d\d)|exceeded retry limit|stream disconnected|"
                               r"error sending request|too many requests|service unavailable|bad gateway|"
                               r"officeqa: corpus not mounted|error response from daemon|"
                               r"cannot connect to the docker daemon", re.I)

_usage = threading.local()
_lock = threading.Lock()
_client = None
_binding = None
_codex_home = None


def _bind(params: dict | None):
    """The target model: agent.params.model (resolved by the framework into a binding), else the default."""
    global _binding
    from hermit.backends import Binding, registry, resolve
    model = (params or {}).get("model")
    _binding = Binding.from_spec(model) if isinstance(model, dict) else resolve(model or DEFAULT_MODEL, registry())


def _chat(messages, max_completion_tokens=MAX_TOKENS, retries=5, stage="rollout", reasoning_effort=None, *,
          tools=None, tool_choice=None, return_message=False, timeout=None):
    """SkillOpt's chat_target_messages, sent to the target's endpoint with the target's reasoning effort. Counts
    tokens and replies cut off at max_tokens for the current rollout."""
    import openai
    global _client
    with _lock:
        if _client is None:
            key_env = _binding.key_env_if_set()
            if key_env and not os.environ.get(key_env):
                raise RuntimeError(f"{key_env} is not set (the key of backend {_binding.backend.name!r})")
            _client = openai.OpenAI(base_url=_binding.base_url, api_key=os.environ.get(key_env) if key_env else "local",
                                    timeout=600, max_retries=0)
    kwargs = {"model": _binding.model, "messages": messages, "max_tokens": min(max_completion_tokens, MAX_TOKENS),
              "extra_body": {"reasoning_effort": EFFORT}}
    if tools:
        kwargs["tools"] = tools
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice
    for attempt in range(retries):
        try:
            response = _client.chat.completions.create(**kwargs)
            break
        except (openai.APIConnectionError, openai.APITimeoutError, openai.RateLimitError,
                openai.InternalServerError) as exc:
            if attempt == retries - 1:
                raise RuntimeError(f"{UNAVAILABLE}: {type(exc).__name__}: {exc}") from None
            time.sleep(min(60, 4 * 2 ** attempt))
    choice = response.choices[0]
    usage = response.usage
    details = getattr(usage, "prompt_tokens_details", None)
    record = {"prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
              "cached_tokens": int(getattr(details, "cached_tokens", 0) or 0),
              "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0)}
    totals = getattr(_usage, "totals", None)
    if totals is not None:
        totals["calls"] += 1
        totals["cut_off"] += choice.finish_reason == "length"
        for key, value in record.items():
            totals[key] += value
    usage_out = {"prompt_tokens": record["prompt_tokens"], "completion_tokens": record["completion_tokens"],
                 "total_tokens": record["prompt_tokens"] + record["completion_tokens"]}
    return (choice.message if return_message else (choice.message.content or "")), usage_out


def _harness(params: dict | None) -> str:
    harness = (params or {}).get("harness") or "direct"
    if harness not in ("direct", "codex"):
        raise ValueError(f"agent.params.harness is direct or codex, not {harness!r}")
    return harness


def _codex_dir(params: dict | None) -> Path:
    return Path(os.path.expanduser(str((params or {}).get("codex_dir") or CODEX_DIR)))


def _setup(params: dict | None = None) -> None:
    from skillopt.envs.officeqa import rollout
    if _binding is None:
        _bind(params)
    rollout.chat_target_messages = _chat
    if _harness(params) == "codex":
        _setup_codex(params)


def _setup_codex(params: dict | None) -> None:
    """SkillOpt's Codex harness (target backend codex_exec) with the target model: its CLI path, whose output the
    trace summary reads, at the target's reasoning effort; Codex's provider settings in a CODEX_HOME of its own."""
    global _codex_home
    from skillopt.model import azure_openai, backend_config
    with _lock:
        if _codex_home is not None:
            return
        home = _codex_dir(params) / f"home-{os.getpid()}"     # one per process: runs side by side share none
        home.mkdir(parents=True, exist_ok=True)
        args = _binding.codex_args()                      # pairs of -c and key=value: TOML lines as they are
        (home / "config.toml").write_text("".join(f"{args[i + 1]}\n" for i in range(0, len(args), 2)))
        os.environ["CODEX_HOME"] = str(home)
        backend_config.set_target_backend("codex_exec")
        os.environ["OFFICEQA_CODEX_CORPUS"] = DATA["docs"]     # the corpus: what codex_sandbox.py mounts read-only
        ready = subprocess.run([str(HERE / "codex_sandbox.py"), "--check"], capture_output=True, text=True)
        if ready.returncode:                                # else every session would fail and score as a wrong answer
            raise RuntimeError(f"the Codex harness's container is not ready: {ready.stderr.strip()}")
        backend_config.configure_codex_exec(path=str(HERE / "codex_sandbox.py"), sandbox="workspace-write",
                                            reasoning_effort=EFFORT, use_sdk="cli", network_access=False,
                                            web_search=False, approval_policy="never")
        azure_openai.TARGET_DEPLOYMENT = _binding.model
        _codex_home = home


def _codex_tokens(pred_dir: Path) -> int:
    """The tokens Codex reports for the run's sessions."""
    raw = pred_dir / "codex_raw.txt"
    text = raw.read_text(errors="replace") if raw.is_file() else ""
    return sum(int(n.replace(",", "")) for n in re.findall(r"^tokens used\n([\d,]+)$", text, re.M))


def _messages(pred_dir: Path) -> list[dict]:
    """The prediction files as chat messages, for Evolvers that read messages."""
    read = lambda name: (pred_dir / name).read_text() if (pred_dir / name).is_file() else ""
    messages = [{"role": "system", "content": read("target_system_prompt.txt")},
                {"role": "user", "content": read("target_user_prompt.txt")}]
    entries = json.loads(read("conversation.json") or "[]")
    for i, e in enumerate(entries[1:]):                  # the first entry is the user prompt
        if e.get("type") == "tool_call":
            call = {"id": f"call_{i}", "type": "function", "function": {"name": e.get("cmd", ""), "arguments": ""}}
            messages += [{"role": "assistant", "content": None, "tool_calls": [call]},
                         {"role": "tool", "tool_call_id": call["id"], "content": e.get("obs", "")}]
        elif e.get("content"):
            messages.append({"role": "assistant", "content": e["content"]})
    return messages


class OfficeQAAgent(Agent):
    """SkillOpt's own rollout with the adapter's context as the skill. `agent.name: ../utils/agent/agent.py:OfficeQAAgent`."""

    def run(self, task: dict, adapter: Path | None) -> RunOutput:
        _setup(self.params)
        from skillopt.envs.officeqa.rollout import process_one

        session = self.session(adapter)
        # The question is the instance's input: what the adapter's context tool, if it declares one, retrieves for.
        skill = session.context(query=task["question"], task=task).strip() if session is not None else ""
        codex = _harness(self.params) == "codex"
        logs = _codex_dir(self.params) / "rollouts" if codex else HERE / ".logs"
        logs.mkdir(parents=True, exist_ok=True)
        item = {"id": task["id"], "question": task["question"], "task_type": task.get("category", "officeqa"),
                "source_files": task["source_files"], "source_docs": task["source_docs"], "ground_truth": ""}
        from skillopt.envs.officeqa.tool_runtime import build_oracle_parsed_pages_context, resolve_docs_roots
        if not build_oracle_parsed_pages_context(item["source_files"], item["source_docs"],
                                                 resolve_docs_roots([DATA["docs"]])):   # cached: the rollout rereads them
            raise RuntimeError(f"no parsed pages for {task['id']} under {DATA['docs']}: see utils/prepare.py")
        started = time.time()
        for attempt in range(3):            # the API was unavailable: run the whole rollout again later
            out_root = Path(tempfile.mkdtemp(prefix=f"{task['id']}-", dir=logs))
            _usage.totals = totals = {"calls": 0, "cut_off": 0, "prompt_tokens": 0, "cached_tokens": 0,
                                      "completion_tokens": 0}
            try:
                result = process_one(item, str(out_root), skill, max_tool_turns=MAX_TURNS,
                                     max_completion_tokens=MAX_TOKENS, search_mode="offline", use_local_tools=True,
                                     data_dirs=[DATA["docs"]])
            finally:
                _usage.totals = None
            failure = result["fail_reason"]
            if UNAVAILABLE not in failure and not (codex and failure.startswith("error: codex exec failed")
                                                   and CODEX_UNAVAILABLE.search(failure)):
                break
            if attempt == 2:
                raise RuntimeError(failure[:4000])
            time.sleep(120)
        pred_dir = out_root / "predictions" / task["id"]
        messages = _messages(pred_dir)
        if codex:
            for link in (pred_dir / "codex_exec" / "docs").glob("root_*"):
                link.unlink()                               # the corpus stays out of the run's log
            shutil.rmtree(out_root / "codex_home", ignore_errors=True)    # codex_sandbox.py's per-rollout CODEX_HOME
            summary = pred_dir / "codex_trace_summary.txt"
            if summary.is_file():                           # what SkillOpt's analyst reads besides the replies
                messages.append({"role": "user", "content": summary.read_text()})
            totals["calls"] = max(len(json.loads((pred_dir / "conversation.json").read_text())) - 1, 0)
        fresh = totals["prompt_tokens"] - totals["cached_tokens"]
        price = _binding.price
        cost = None if not price or codex else round((fresh * price.get("input", 0)       # Codex reports totals only
                                             + totals["cached_tokens"] * price.get("cached_input", price.get("input", 0))
                                             + totals["completion_tokens"] * price.get("output", 0)) / 1e6, 6)
        tokens = _codex_tokens(pred_dir) if codex else totals["prompt_tokens"] + totals["completion_tokens"]
        answer = result["predicted_answer"] if result["agent_ok"] else ""
        extra = {"model": _binding.ref, "harness": "codex" if codex else "direct", "reasoning_effort": EFFORT,
                 "cut_off_replies": totals["cut_off"], "oracle_pages_chars": result["oracle_parsed_pages_chars"],
                 "turns": result["n_turns"]}
        if codex:
            # SkillOpt reports a session past its 180 s as "timed out after" or, from its own timeout handler (it writes
            # Codex's output as bytes), as "write() argument must be str, not bytes"
            extra["session_timed_out"] = any(s in result["fail_reason"] for s in ("timed out after", "str, not bytes"))
        if session is not None and session.last_context is not None:
            extra["context_tool"] = session.last_context      # what the adapter's context tool rendered for this question
        return RunOutput(output={"answer": answer, "fail_reason": "" if result["agent_ok"] else result["fail_reason"]},
                         log=pred_dir, messages=messages, cost_usd=cost, tokens=tokens,
                         wall_s=round(time.time() - started, 1), api_speed={"model_calls": totals["calls"]}, extra=extra)
