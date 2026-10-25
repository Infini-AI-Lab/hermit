"""OfficeQA in the SkillOpt paper's setting, answered by Codex with a Harness² Codex harness.

One rollout is one `codex exec` session in a workspace that holds the question as `task.md` (the question with its
oracle parsed pages and its source files as hints, the text SkillOpt's own Codex harness writes), the whole corpus
under `docs/` (read-only), and the adapter's harness tree deployed as Codex reads it (google-research/harness2,
`CodexSubstrate`): `AGENTS.md`, `.agents/skills/`, `scripts/` and `.codex/hooks.json` at the workspace root, and
`sub_agents/<name>/AGENT.md` as agent roles in the session's CODEX_HOME. The session runs in a Docker container of
its own (`codex_session.py`) that sees only the workspace, that home and the corpus and reaches only the model
provider; Codex's workspace-write sandbox runs inside it, so the session's commands have no network at all.

    agent:
      name: ../utils/agent/codex_agent.py:OfficeQACodexAgent
      concurrency: 2
      params:
        model: fireworks/accounts/fireworks/models/glm-5p3-flash   # <backend>/<model>; the backend must serve the Responses API
        effort: medium                 # Codex's model_reasoning_effort
        timeout_s: 3600                # one session's wall clock (the paper's cap; its 200-turn cap is no Codex setting)
        codex_dir: ~/.cache/hermit/officeqa-h2-codex   # workspaces, homes and logs, outside any checkout
        docs: null                     # the corpus; default ../data.json's docs (utils/prepare.py)

The answer is the text inside the last `<answer>...</answer>` of Codex's final message; a session that ends without
one answers nothing, and `fail_reason` says why. A session the provider could not serve (429, 5xx) is run again
later, up to three times; a container that could not start raises. The rollout's log is a directory:
`agent-codex.jsonl` (the `codex exec --json` event stream, the trajectory), `task.md`, `prompt.txt`,
`last_message.txt` and `harness.json` (what the adapter deployed). The workspace stays beside it for inspection,
without its links into the corpus.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
import tomllib

from hermit.agents import Agent
from hermit.task import RunOutput

HERE = Path(__file__).resolve().parent
DEFAULT_MODEL = "fireworks/accounts/fireworks/models/glm-5p3-flash"
EFFORT = "medium"                                                 # paper p.8: teacher and student at medium
TIMEOUT_S = 3600
CODEX_DIR = "~/.cache/hermit/officeqa-h2-codex"
ROOT_ENTRIES = ("AGENTS.md", ".agents", "scripts", ".codex")      # upstream's codex_root install (CODEX_ROOT_ENTRIES)
ANSWER = re.compile(r"<answer>(.*?)</answer>", re.IGNORECASE | re.DOTALL)
PROMPT = ("Read `task.md`, inspect or search the full OfficeQA corpus under `docs/`, and answer the question.\n"
          "Treat candidate files in `task.md` as hints, not an access limit.\n"
          "Return the final answer inside <answer>...</answer>.")
CORPUS_NOTE = ("The full OfficeQA document corpus is available under `docs/`. The candidate files below are source "
               "hints or likely starting points; search the full corpus if needed.")
# Codex's own reports of a provider it could not reach (it retries those itself first), and of a container that
# could not start, not the session's text
UNAVAILABLE = re.compile(r"unexpected status (?:429|5\d\d)|exceeded retry limit|stream disconnected|"
                         r"error sending request|too many requests|service unavailable|bad gateway|"
                         r"officeqa: corpus not mounted|error response from daemon|"
                         r"cannot connect to the docker daemon", re.I)


def task_text(task: dict, docs: str) -> tuple[str, list[str]]:
    """`task.md` as SkillOpt's Codex harness writes it (its `_build_user`: the question, its oracle parsed pages,
    the corpus note, the candidate files as workspace paths, the source hints), and the corpus roots to link."""
    from skillopt.envs.officeqa.rollout import _build_user, _workspace_doc_path
    from skillopt.envs.officeqa.tool_runtime import (build_oracle_parsed_pages_context, resolve_candidate_files,
                                                     resolve_docs_roots)
    roots = resolve_docs_roots([docs])
    oracle = build_oracle_parsed_pages_context(task["source_files"], task["source_docs"], roots)
    if not oracle:
        raise RuntimeError(f"no parsed pages for {task['id']} under {docs}: see utils/prepare.py")
    files = resolve_candidate_files(task["source_files"], roots)
    item = {"id": task["id"], "question": task["question"], "task_type": task.get("category", "officeqa"),
            "source_files": task["source_files"], "source_docs": task["source_docs"]}
    text = _build_user(item, [_workspace_doc_path(f, roots) for f in files[:20]], corpus_note=CORPUS_NOTE,
                       oracle_context=oracle)
    return text, roots


def frontmatter(text: str) -> tuple[dict[str, str], str]:
    """A file's YAML-ish frontmatter as strings, and its body ({} without one)."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end < 0:
        return {}, text
    head: dict[str, str] = {}
    for line in text[4:end].splitlines():
        key, sep, value = line.partition(":")
        if sep and re.fullmatch(r"[A-Za-z_][\w-]*", key.strip()):
            head[key.strip()] = value.strip().strip("\"'")
    body = text[end + 4:]
    return head, body[body.find("\n") + 1:] if "\n" in body else ""


def deploy_harness(adapter: Path | None, work: Path) -> dict:
    """The adapter's Codex tree into the workspace root, as upstream installs it; the sub_agents are returned for
    the home's config (name, description, instructions). Without an adapter the workspace carries no harness."""
    out: dict = {"agents_md": False, "skills": [], "scripts": [], "hooks": False, "sub_agents": []}
    if adapter is None:
        return out
    adapter = Path(adapter)
    for entry in ROOT_ENTRIES:
        src = adapter / entry
        if src.is_dir():
            shutil.copytree(src, work / entry, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        elif src.is_file():
            shutil.copy2(src, work / entry)
    out["agents_md"] = (work / "AGENTS.md").is_file()
    out["skills"] = sorted(p.parent.name for p in (work / ".agents" / "skills").glob("*/SKILL.md"))
    out["scripts"] = sorted(p.name for p in (work / "scripts").glob("*.py"))
    out["hooks"] = (work / ".codex" / "hooks.json").is_file()
    for agent_md in sorted((adapter / "sub_agents").glob("*/AGENT.md")):
        meta, body = frontmatter(agent_md.read_text(encoding="utf-8"))
        name = re.sub(r"[^a-z0-9_]", "_", agent_md.parent.name.lower())
        out["sub_agents"].append({"name": name, "description": meta.get("description") or name,
                                  "instructions": body.strip()})
    return out


def write_home(home: Path, binding, sub_agents: list[dict]) -> list[str]:
    """CODEX_HOME/config.toml: the provider (the binding's Codex settings), no shell snapshot, the session's shell
    given only Codex's core variables (no key, no proxy), no network for its commands, and the adapter's sub_agents
    as agent roles. Returns the key variables the container must receive, by name."""
    args = binding.codex_args()
    lines = [args[i + 1] for i in range(0, len(args), 2)]            # Codex's -c pairs are TOML lines
    lines += ["features.shell_snapshot = false", "sandbox_workspace_write.network_access = false"]
    if sub_agents:
        lines.append("features.multi_agent = true")
    lines += ["", "[shell_environment_policy]", 'inherit = "core"', 'exclude = ["*KEY*", "*TOKEN*", "*SECRET*"]']
    (home / "agents").mkdir(parents=True, exist_ok=True)
    for role in sub_agents:
        spec = "\n".join(f"{k} = {json.dumps(v, ensure_ascii=False)}" for k, v in
                         {"name": role["name"], "description": role["description"],
                          "developer_instructions": role["instructions"]}.items()) + "\n"
        tomllib.loads(spec)
        (home / "agents" / f"{role['name']}.toml").write_text(spec, encoding="utf-8")
        lines += ["", f"[agents.{role['name']}]", f"description = {json.dumps(role['description'], ensure_ascii=False)}",
                  f"config_file = {json.dumps('agents/' + role['name'] + '.toml')}"]
    text = "\n".join(lines) + "\n"
    tomllib.loads(text)
    (home / "config.toml").write_text(text, encoding="utf-8")
    key_env = binding.key_env_if_set()
    return [key_env] if key_env else []


def events_of(log: Path) -> list[dict]:
    out = []
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines() if log.is_file() else []:
        line = line.strip()
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


class OfficeQACodexAgent(Agent):
    """`agent.name: ../utils/agent/codex_agent.py:OfficeQACodexAgent`."""

    def _binding(self):
        if not hasattr(self, "_bound"):
            from hermit.backends import Binding, registry, resolve
            model = self.params.get("model")
            self._bound = Binding.from_spec(model) if isinstance(model, dict) \
                else resolve(model or DEFAULT_MODEL, registry())
        return self._bound

    def _docs(self) -> str:
        docs = self.params.get("docs")
        if not docs:
            docs = json.loads((HERE.parent / "data.json").read_text())["docs"]
        return str(self.path(docs))

    def _ready(self, corpus: Path) -> None:
        if getattr(self, "_checked", False):
            return
        from codex_session import check        # beside this file; the loader puts it on sys.path
        problems = check(corpus)
        if problems:                               # else every session would fail and score as a wrong answer
            raise RuntimeError("the Codex harness's container is not ready: " + "; ".join(problems))
        self._checked = True

    def run(self, task: dict, adapter: Path | None) -> RunOutput:
        import sys
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import codex_session
        from hermit.evolver.agent import parse_log

        binding = self._binding()
        docs = self._docs()
        corpus = Path(docs).resolve()
        self._ready(corpus)
        text, roots = task_text(task, docs)
        effort = str(self.params.get("effort") or EFFORT)
        timeout_s = float(self.params.get("timeout_s") or TIMEOUT_S)
        rollouts = Path(os.path.expanduser(str(self.params.get("codex_dir") or CODEX_DIR))) / "rollouts"
        rollouts.mkdir(parents=True, exist_ok=True)
        # Resolved: the container binds each directory at its real path, and Codex is given these paths.
        ws = Path(tempfile.mkdtemp(prefix=f"{task['id']}-", dir=rollouts)).resolve()
        work, home, log = ws / "work", ws / "home", ws / "log"
        started = time.time()
        for attempt in range(3):            # the provider was unavailable: run the whole session again later
            for d in (work, home, log):
                shutil.rmtree(d, ignore_errors=True)
                d.mkdir(parents=True)
            deployed = deploy_harness(adapter, work)
            keys = write_home(home, binding, deployed["sub_agents"])
            (work / "task.md").write_text(text, encoding="utf-8")
            # The corpus is bound into docs/root_1 by the container (a real directory there, so `find docs` and
            # `rg --files docs` list it; a link they would not follow); further roots, if any, are linked.
            (work / "docs" / "root_1").mkdir(parents=True)
            for i, root in enumerate(roots[1:], start=2):
                (work / "docs" / f"root_{i}").symlink_to(Path(root).resolve())
            args = ["exec", "--json", "--skip-git-repo-check", "-s", "workspace-write", "-C", str(work),
                    "-m", binding.model, "-c", f'model_reasoning_effort="{effort}"',
                    "-c", 'approval_policy="never"',          # as SkillOpt's Codex harness: never wait for an approval
                    "--dangerously-bypass-hook-trust", "-o", str(work / ".last_message.txt"),
                    *(["--enable", "multi_agent"] if deployed["sub_agents"] else []), PROMPT]
            code, timed_out = codex_session.run(args, work=work, home=home, corpus=corpus, keys=keys,
                                                timeout=timeout_s, stdout=log / "agent-codex.jsonl",
                                                corpus_at=(Path(roots[0]), work / "docs" / "root_1"))
            events = events_of(log / "agent-codex.jsonl")
            served = any(e.get("type") == "turn.completed" for e in events)
            raw = (log / "agent-codex.jsonl").read_text(encoding="utf-8", errors="replace")
            if served or timed_out or not UNAVAILABLE.search(raw):
                break
            if attempt == 2:
                raise RuntimeError(f"codex exec could not be served: {raw[-2000:]}")
            time.sleep(120)
        last_file = work / ".last_message.txt"
        last = last_file.read_text(encoding="utf-8", errors="replace") if last_file.is_file() else ""
        if not last.strip():
            texts = [(e.get("item") or {}).get("text") or "" for e in events
                     if e.get("type") == "item.completed" and (e.get("item") or {}).get("type") == "agent_message"]
            last = texts[-1] if texts else ""
        found = ANSWER.findall(last)
        answer = found[-1].strip() if found else ""
        if answer:
            fail = ""
        elif timed_out:
            fail = f"session timed out after {int(timeout_s)} s without an <answer>"
        elif code:
            fail = f"codex exec exited {code} without an <answer>: {raw[-1500:].strip()}"
        else:
            fail = "no <answer>...</answer> in the final message"
        # The log stays self-contained; the workspace stays for inspection, without its links into the corpus.
        shutil.copy2(work / "task.md", log / "task.md")
        (log / "prompt.txt").write_text(PROMPT, encoding="utf-8")
        (log / "last_message.txt").write_text(last, encoding="utf-8")
        (log / "harness.json").write_text(json.dumps({**deployed, "sub_agents": [r["name"] for r in
                                                                                  deployed["sub_agents"]]}, indent=1))
        shutil.rmtree(work / "docs", ignore_errors=True)       # the mount point and any links: the corpus stays out
        _, usage = parse_log(log / "agent-codex.jsonl", "codex", binding.ref)
        u = usage[0] if usage else None
        price = binding.price
        cost = None if not (u and price) else round(((u.input_tokens - u.cached_input_tokens) * price.get("input", 0)
                                                     + u.cached_input_tokens * price.get("cached_input", price.get("input", 0))
                                                     + u.output_tokens * price.get("output", 0)) / 1e6, 6)
        actions = sum(1 for e in events if e.get("type") == "item.completed"
                      and (e.get("item") or {}).get("type") in ("command_execution", "mcp_tool_call", "file_change"))
        return RunOutput(output={"answer": answer, "fail_reason": fail}, log=log,
                         cost_usd=cost, tokens=(u.input_tokens + u.output_tokens) if u else None,
                         wall_s=round(time.time() - started, 1), api_speed={"model_calls": u.calls if u else 0},
                         extra={"model": binding.ref, "harness": "codex", "reasoning_effort": effort,
                                "turns": u.calls if u else 0, "actions": actions, "exit_code": code,
                                "session_timed_out": timed_out, "workspace": str(work),
                                "deployed": {k: v for k, v in deployed.items() if k != "sub_agents"}
                                | {"sub_agents": [r["name"] for r in deployed["sub_agents"]]}})
