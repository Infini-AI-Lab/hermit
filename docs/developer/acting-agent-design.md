# Proposed: one acting agent per host

This page is a design record, not supported configuration. Nothing below is implemented; the current acting agents
are described in [Task and acting pipeline](../framework/task.md#built-in-agents) and in each example's own code.

## The problem

An acting agent runs a task with an adapter and returns what the judge scores (`Agent.run(task, adapter) ->
RunOutput`, `hermit/agents/base.py`). For a coding-agent CLI such as Codex, most of that work does not depend on the
task: deploy the adapter, start the CLI in an isolated workspace, authenticate it, stop it at a time limit, run it
again when the provider could not serve it, and keep what it did. Today that work is written once per example, and
twice for OfficeQA:

| | `agent.py`, `harness: codex` | `codex_agent.py` | `hermit/agents/cli.py` (`agent.name: codex`) |
|---|---|---|---|
| Written for | SkillOpt's paper setting: calls SkillOpt's own rollout | Harness² (a Codex tree, 3,600 s sessions) | the framework's built-in |
| Used by | GEPA, SkillOpt; the Flash row's no-skill and initial-skill rows; ACE (#48) | Harness², Continual Harness, ReHarness's Codex stream, Meta-Harness Adapter | no example |
| The adapter reaches Codex as | `.agents/skills/skillopt-target/SKILL.md`, and a prompt line asking Codex to use it | `AGENTS.md` (or the whole Codex tree, or every adapter file) | what `hermit deploy` writes |
| Time | 180 s per session, up to 24 sessions while no `<answer>` | one session, 3,600 s | one session, `timeout_s` |
| Isolation | a container per session (`codex_sandbox.py`, Codex 0.134 by default) | a container per session (`codex_session.py`, Codex 0.162 by default) | none of its own |
| ChatGPT login | added in #48 | no | no |
| What it keeps | SkillOpt's prediction files: `conversation.json`, `codex_raw.txt`, `codex_trace_summary.txt` | Codex's event stream `agent-codex.jsonl`, `task.md`, `harness.json` | Codex's event stream |

Both OfficeQA wrappers run the official `codex exec` with the same `task.md` and almost the same prompt; they differ in
the four rows above it. Three things follow:

1. **The same model and adapter score differently by wrapper**, and results from both sit in one table. The recipe's
   Flash row takes its no-skill (0.541) and initial-skill (0.576) rows, and GEPA's and SkillOpt's, from
   `agent.py`, and Harness², Continual Harness and Meta-Harness Adapter from `codex_agent.py`;
   `examples/officeqa/harness2_stream/README.md` calls them "the same executor".
2. **A capability lands in one wrapper.** The ChatGPT login of #48 reaches `agent.py` only; running a no-skill
   baseline with it on `codex_agent.py` needs the same code written again.
3. **The Evolvers depend on the wrapper.** SkillOpt's Evolver reads `conversation.json` and `codex_trace_summary.txt`,
   which only SkillOpt's harness writes; from `codex_agent.py` its learner sees no trajectory. GEPA's falls back to
   concatenating every file of the log, raw JSON included. Continual Harness reads `codex_agent.py`'s extra fields
   (`session_timed_out`, `exit_code`), which `cli.py` names `timed_out`. Codex's event stream is parsed three times
   (`reharness/trajectory.py`, `continual_harness/evolver.py`, and Harness²'s upstream `render_events`).

Benchmarks whose own loop is the agent (CL-Bench's and ServeLearnBench's agents play the benchmark's loop with the
adapter plugged in) are not part of the problem and keep their agents.

## The proposal: three layers

| Layer | Owns | Written by |
|---|---|---|
| Task | what the workspace holds for a task (files, read-only data), the prompt, how the answer is read from the agent's last message, what of the workspace is kept, the judge | each example: config keys, and functions named in the config when the workspace is built in code; no CLI code |
| Host agent | deploying the adapter, running the CLI, isolation, authentication, time limit, retries, the record | the framework: one per host (`codex`, `claude-code`) |
| Evolver | what its learner sees of a record | each method, on a shared parser |

The rule between the last two: **the agent records, the Evolver interprets.** The agent keeps the host's own log
unchanged plus a fixed set of fields; it never writes a method's view. A method that wants a summary builds it from
the record.

## The host agent

`hermit/agents/cli.py` grows into the host agent; `agent.name: codex` and `claude-code` keep their meaning. A task
customizes it with configuration and, where configuration is not enough, with functions it names in the
configuration, as `judge.path` and a function agent are named today (`file.py:function`).

**Configuration**, for a task whose workspace is a prompt and some files:

```yaml
agent:
  name: codex
  params:
    model: gpt-6-luna                 # a Codex model name with `login`, else a <backend>/<model> reference
    login: chatgpt                    # absent: the backend's key
    effort: medium
    timeout_s: 3600
    prompt: environment/prompt.md     # `{field}` placeholders take the task's fields
    files: [environment/fixtures]     # copied into every workspace
    mounts: [{source: data/corpus, at: docs, readonly: true}]
    answer: '<answer>(.*?)</answer>'  # the last match in the last message; absent: the whole message
    isolation: container              # container | sandbox | none
    retries: 3                        # runs the provider could not serve (429, 5xx), then an error
```

**Function hooks**, for a task that builds its workspace in code. OfficeQA writes `task.md` from the question's oracle
pages and binds the corpus:

```yaml
agent:
  name: codex
  params:
    prompt: ../utils/prompt.md
    prepare: ../utils/task.py:prepare     # before the session
    answer: ../utils/task.py:answer       # after it; in place of the `answer` pattern
    collect: ../utils/task.py:collect     # after it, optional
```

```python
# examples/officeqa/utils/task.py
def prepare(task: dict, workspace: Path, params: dict) -> list[dict]:
    """Write the task's files into the workspace; return the read-only mounts, as `mounts` takes them."""
    corpus = Path(params["docs"])
    (workspace / "task.md").write_text(task_text(task, corpus))
    return [{"source": corpus, "at": "docs/root_1", "readonly": True}]

def answer(last_message: str, task: dict) -> dict:
    """What the judge scores."""
    found = ANSWER.findall(last_message)
    return {"answer": found[-1].strip() if found else ""}

def collect(task: dict, workspace: Path, log: Path) -> dict:
    """Copy the task's own artifacts from the workspace into the log; return facts to record."""
    return {}
```

| Hook | Called | Takes | Returns |
|---|---|---|---|
| `prepare` | before the session, after the adapter is deployed | the task, the workspace, `agent.params` | the mounts, added to `mounts` |
| `prompt` | before the session; a template file, or a function | the task | the prompt |
| `answer` | after the session; a pattern, or a function | the last message, the task | `RunOutput.output`, to which the agent adds `fail_reason` |
| `collect` | after the session | the task, the workspace, the log directory | facts recorded as `RunOutput.extra["task"]` |

A `prompt` or `answer` value of the form `file.py:function` names a function, relative to the config's directory;
any other value is a template file or a pattern.

The hooks are the task's only code. They run on the machine's side, outside the session's container, and see the
task, the workspace and the log, never the session itself: deployment, the container, the login, the retries and
the record stay the agent's, and no hook changes the host's log or the fixed `extra` keys. What a hook adds
(`collect`'s files, `answer`'s fields) appears in the learning material beside the host's log, for every Evolver
alike.

**A subclass** of the host agent is the fallback, for a run that must change how a session runs, not what the
workspace holds: SkillOpt's paper protocol, which runs Codex again with its previous reply while no `<answer>`
came, is one. It keeps the record as the base class writes it, and its results are labelled with it.

What the base class does for every task:

- **Deploy.** `deploy: host` (the default) writes what `hermit deploy` writes. `deploy: tree` copies a Codex tree
  (`AGENTS.md`, `.agents/`, `scripts/`, `.codex/hooks.json`, `sub_agents/` as roles, `memory/`), as
  `codex_agent.py` does for Harness² and Continual Harness. `deploy: files` places `ADAPTER.md` as `AGENTS.md` and
  every other adapter file at its path, refusing the names the session uses and `.codex/`, as Meta-Harness Adapter
  needs. An adapter with a `context_tool` gets `Session.context(query=..., task=...)` as its `AGENTS.md`, with the
  query field named by `context_query`; `cli.py` refuses such adapters today.
- **Isolate.** `isolation: container` runs each session in a container of its own, as `codex_session.py` does: the
  workspace and the session's home read-write, the mounts read-only, an internal network whose only way out is an
  egress proxy to the provider's hosts, and Codex's own workspace-write sandbox inside. The image and the proxy are
  configured once (`agent.params.container` or environment variables), not per example. `sandbox` uses the run's
  bubblewrap sandbox; `none` is for trusted local runs.
- **Authenticate.** With a backend, the binding's provider settings and key variable, as now. With `login:
  chatgpt`, #48's mechanism: the run keeps a copy of `~/.codex/auth.json`, each session's home starts from it, and
  a token a session refreshed is written back under a lock. The proxy's hosts follow from the choice
  (`chatgpt.com`, `auth.openai.com` for the login).
- **Stop and retry.** One session per run, stopped at `timeout_s`. A session the provider could not serve is run
  again after a wait, up to `retries`; after that the run is an error, not a wrong answer.
- **Record.** `RunOutput.log` is a directory with the host's own log (`agent-codex.jsonl`, `agent-claude.jsonl`),
  the prompt, the last message and the deployment record. `RunOutput.output` is what `answer` returned plus
  `fail_reason`. `RunOutput.extra` carries the same keys for every host: `backend`, `model`, `effort`, `exit_code`,
  `timed_out`, `attempts`, `deployed`, `context_tool` (when used), `workspace`.

## Trajectories: one parser, each Evolver's view

A new framework module reads a learning-material record into a host-neutral list of steps:

```python
from hermit.trajectory import read

steps = read(record, step_dir)   # [Message(role, text), Reasoning(text), Command(command, exit_code, output),
                                 #  ToolCall(name, arguments, result), FileChange(paths)], in order
```

It reads Codex's event stream, Claude Code's stream-json and `RunOutput.messages`, and gives the common rendering
helpers (a text view with clipped outputs, `clip_middle`). Each Evolver then decides what its learner sees:

| Evolver | Today | Proposed |
|---|---|---|
| SkillOpt | SkillOpt's harness's `conversation.json` and `codex_trace_summary.txt` | builds the trace summary from the steps, in SkillOpt's format (`trajectory_view: paper` stays the Evolver's setting) |
| GEPA | messages, else every log file concatenated | the steps' text view |
| ReHarness, Continual Harness | their own Codex stream parsers | the shared parser; their rendering unchanged |
| Harness² (upstream) | upstream's `render_events` | unchanged: it wants upstream's view |
| Agent Evolvers, Meta-Harness Adapter, MWM | the learner reads the files itself | unchanged; the learning material's README names the host logs |

## Migration

1. **Core: the trajectory module**, with fixtures cut from real Codex and Claude Code streams.
2. **Core: the host agent**, with the function hooks, the container runner, the login, retries, the deploy modes,
   the context tool and the fixed `extra` keys; tested with a fake CLI on `PATH`, as `tests/test_sandboxed_evolvers.py`
   tests the agent Evolvers.
3. **Evolvers** on the shared parser: SkillOpt, GEPA, ReHarness, Continual Harness.
4. **OfficeQA**: `utils/task.py` with `prepare` and `answer`, and a prompt file, replace `agent.py`'s Codex mode,
   `codex_agent.py`, `codex_sandbox.py` and `codex_session.py`; `agent.py` keeps `harness: direct`. Every Codex
   config names `agent.name: codex` with those hooks. #48 (ACE) is merged first, since it changes `agent.py` and
   `codex_sandbox.py`.
5. **Re-measurement**, below; then the GPT-6-Luna baselines (no skill and the initial skill, one pass) on the new
   agent.

`examples/lean_swarm/launch.py`, which starts its own Codex sessions outside an agent, can move later.

## What changes in results

- The SkillOpt protocol in Codex (180 s sessions, the skill as `SKILL.md`, up to 24 sessions) is no longer run.
  The recipe's Codex-harness table (Codex 0.134, three passes) and the Flash row's GEPA, SkillOpt, no-skill and
  initial-skill rows were made with it. They stay published, labelled with the wrapper that made them; the recipe
  states for every row which wrapper ran it.
- Under one wrapper the Flash row needs its no-skill and initial-skill rows again (GLM 5.3 Flash, one pass, 2 × 172
  sessions), compared per question with the old ones.
- SkillOpt's learner reads a summary the Evolver rebuilt instead of SkillOpt's own; a training run on the new agent
  is labelled as SkillOpt on Hermit's Codex agent, not the paper's protocol.

## Open questions

- Deploy modes as agent parameters, or declared by the template (a Codex-tree template saying how it deploys)?
- Is the answer the agent's to extract (`answer`), or the judge's, given the whole last message?
- The container runner: Docker only, or Podman as well; does the framework ship the image's Dockerfile and the egress
  proxy (today under `examples/officeqa/utils/agent/docker/`)?
- A login for Claude Code, by the same mechanism.
- The learning side runs Codex too, with code of its own: the agent Evolvers (`hermit/evolver/agent.py`), Harness²'s
  editors, ACE's Reflector and Curator (#48). Do they use the host agent's container, login and record later?
- Are GEPA and SkillOpt retrained on the new agent for the Flash row, or are their rows kept with their label?
