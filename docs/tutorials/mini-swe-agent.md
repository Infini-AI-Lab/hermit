# Use mini-swe-agent as the acting agent

[mini-swe-agent](https://mini-swe-agent.com) is a minimal agent: its model runs bash commands and nothing else, and
one YAML config's templates make its messages. Hermit runs it two ways: as the acting agent of `check`, `eval` and
`train` (`agent.name: mini-swe-agent`), and as a deployment host, `hermit deploy --host mini-swe-agent`, a workspace
you run `mini` in. Both build the same messages, so a deployed adapter gives the agent what it had in training.

## 1. Install

From the checkout root, in the environment that runs Hermit (pip 25.1+):

```bash
python -m pip install -e . --group mini-swe-agent
```

The group pins mini-swe-agent 2.4.6, which brings litellm.

## 2. How an adapter reaches the agent

| Adapter part | What mini-swe-agent gets |
|---|---|
| Context (`ADAPTER.md`) | The system message, under "Instructions from the harness adapter" |
| Per-task context (`context_tool`) | Appended to that context for each task, by the acting agent; a deployed workspace has the static context only |
| Tools | One bash command, `hermit-tool <name> '<JSON arguments>'`, answered through the adapter's Session; the system message lists each tool with its argument schema |
| Skills | Copied to `.hermit/skills/` in the workspace; the system message lists them |
| Roles | Not used by the acting agent; refused by `deploy` (mini-swe-agent has no subagents) |

`hermit-tool` is not a program on `PATH`: Hermit's environment class (`hermit.hosts.mini_swe_agent_env.HermitEnvironment`)
recognizes a command whose first word is `hermit-tool` and answers it in mini-swe-agent's process. It must be a
command of its own; `hermit-tool` alone lists the tools. Every other command runs as in mini-swe-agent's local
environment.

## 3. Configure the acting agent

```yaml
model: deepseek/deepseek-flash         # <backend>/<model id>, see Model backends
agent:
  name: mini-swe-agent
  concurrency: 3
  params:
    model_class: litellm              # or litellm_textbased: one ```mswea_bash_command block per response
    config: agent/mini.yaml           # optional: a mini-swe-agent config merged over Hermit's
    user: "{question}"                # the task text (default: the task's prompt, question, instruction or input)
    files: [environment/data]         # copied into every workspace before the run
    env: {}                           # variables for the agent's commands
    timeout_s: 3600
    workspaces: .logs/workspaces
```

Each task runs in a fresh workspace under `workspaces`. The agent submits its answer as mini-swe-agent ends a task,
`echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && cat answer.txt`, and the submission is the output the judge scores.
Without one (a step limit, the time limit) the output is the last assistant message. The workspace is the log: the
trajectory is `trajectory.traj.json`, and `RunOutput.messages` holds it as chat-completions messages for the Evolvers
that read them.

The model is a [backend reference](../guides/model-backends.md); mini-swe-agent calls it through litellm, at the
backend's URL with its key. The key is passed with each call: it is in neither the trajectory nor the commands'
environment, which holds `env` (a `$NAME` value is expanded, and not recorded) and a few basic variables (`PATH`,
`HOME`, `LANG`, ...) only. That keeps the key out of what the agent reads, but it does not confine the commands: they
run as you, with your files. Only the run's sandbox (`sandbox.enabled: true`, Linux with bubblewrap) confines them.

Tokens are read from the responses and priced at the backend's `prices`; without a price the cost is unknown.
mini-swe-agent's own cost tracking is off, so its `cost_limit` never stops a task: `agent.step_limit` (50 model
calls) and `timeout_s` do. The wall time is checked before each model call, one request is bounded by 600 seconds
(or `timeout_s`), and a command by the environment's `timeout`. A wrong key, model or setting stops the run; any
other model error (a context overflow, retries exhausted) ends the task, scored on what it did, with the error's
name in `extra["exit_status"]`.

`config` is a mini-swe-agent config file, merged over Hermit's the way `mini -c` merges files: change
`agent.step_limit`, the templates, `model.model_kwargs` (temperature, `max_tokens`) or the `environment` section
(`timeout` of a command, more `env`). A system template of your own receives the adapter's context where it says
`{{ adapter_context }}`, or at its end. Set the model with `params.model` and `model_class`, not in the file. Another
`environment_class` (docker, ...) is passed to mini-swe-agent for a run with no adapter tools, skills or `files`:
those reach the local workspace only.

## 4. Run the example

`docs/_examples/mini_swe_agent/` has nine questions about a small CSV file and a judge that accepts exactly
`{"answer": <integer>}`, with GEPA as the learner (`--group gepa`). It uses DeepSeek for both:

```bash
python -m pip install -e . --group mini-swe-agent --group gepa
export DEEPSEEK_API_KEY=...
mkdir -p .demo && cp -R docs/_examples/mini_swe_agent .demo/mini-swe-agent
hermit check --config .demo/mini-swe-agent/run.yaml
hermit eval --config .demo/mini-swe-agent/run.yaml --adapter none
hermit train --config .demo/mini-swe-agent/run.yaml
hermit eval --config .demo/mini-swe-agent/run.yaml --adapter final
```

Another backend is one override, for example `model=openai/<model id>`. The tasks need the file and a few commands; the
format is what there is to learn, and a strong model may answer in it from the start. **There is no promised score
gain.** The example turns the sandbox off so that it runs on macOS too: the agent's commands then run unconfined, as
you. On Linux with bubblewrap, `sandbox.enabled=true` runs each task in the rollout sandbox.

## 5. Deploy to a mini-swe-agent workspace

```bash
hermit deploy --adapter .demo/mini-swe-agent/runs/mini-swe-agent/checkout --workspace .demo/mini-workspace \
  --host mini-swe-agent
cd .demo/mini-workspace
mini -c mini-swe-agent.yaml -m deepseek/deepseek-flash
```

The workspace holds `mini-swe-agent.yaml` (Hermit's prompts over mini-swe-agent's `mini` config, the adapter's
context written into the system template), `.hermit/skills/` and `.hermit-deploy.json`. An adapter with tools also
names Hermit's environment class and the adapter in the config's `environment` section, and every tool call is
appended to `tool-calls.jsonl`; run `mini` from the Python environment Hermit is installed in. The model and the
limits are mini-swe-agent's own settings (`-m`, its keys, global config and `cost_limit`), as for any `mini` run. Redeploying replaces the
config; the workspace must be empty or generated for the same host.
