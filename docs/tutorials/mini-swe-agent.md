# Use mini-swe-agent as the acting agent

[mini-swe-agent](https://mini-swe-agent.com) is a minimal agent: its model runs bash commands and nothing else, and
one YAML config's templates make its messages. In Hermit it is a Base Agent beside Codex and Claude Code
([Agent layers](../framework/agent-layers.md)): `agent.name: mini-swe-agent` acts in `check`, `eval` and `train`, and
`hermit deploy --host mini-swe-agent` writes a workspace you run `mini` in. With Hermit's prompts both give the
agent the same system message, so a deployed adapter reads what it read in training.

## 1. Install

From the checkout root, in the environment that runs Hermit:

```bash
python -m pip install -e ".[mini-swe-agent]"
```

The extra pins mini-swe-agent 2.4.6, which brings litellm.

## 2. How an adapter reaches the agent

The acting agent deploys the adapter into each task's workspace as `hermit deploy --host mini-swe-agent` does: the
whole adapter is copied to `hermit/<id>/`, and `mini-swe-agent.yaml` holds what mini-swe-agent can take.

| Adapter part | What mini-swe-agent gets |
|---|---|
| Context (`ADAPTER.md`), and a `context_tool`'s text for the task | The system message, under "Instructions from the harness adapter" |
| Tools | One bash command, `hermit-tool <name> '<JSON arguments>'`; the system message lists each tool with its argument schema |
| Skills, roles, hooks, other files | Named in the system message with their paths in `hermit/<id>/` and what to do with them: mini-swe-agent loads no skills, starts no sub-agents and runs no hooks |

`hermit-tool` is not a program on `PATH`: Hermit's environment class recognizes a command whose first word is
`hermit-tool` and answers it in mini-swe-agent's process. It must be a command of its own; `hermit-tool` alone lists
the tools. Every other command runs as in mini-swe-agent's local environment.

## 3. Configure the acting agent

```yaml
model: deepseek/deepseek-flash          # <backend>/<model id>, see Model backends
agent:
  name: mini-swe-agent
  concurrency: 3
  params:
    web_search: false                   # set either way for every CLI agent; mini-swe-agent has none
    model_class: litellm                # or litellm_textbased: one ```mswea_bash_command block per response
                                        # (a <backend>/<model> reference takes these two only)
    config: agent/mini.yaml             # optional: a mini-swe-agent config merged over Hermit's
    step_limit: 50                      # model calls per task (the config's agent.step_limit wins)
    prompt: "{question}"                # the task text (default: the task's prompt, question, instruction or input)
    files: [environment/data]           # copied into every workspace before the run
    env: {}                             # variables for the agent's commands (a `$NAME` value is expanded)
    timeout_s: 3600
```

The per-layer form says the same: `agent.base: mini-swe-agent`, the model as `agent.model`, and mini-swe-agent's own
settings in `agent.mini-swe-agent`. The task functions of the [CLI agents](../framework/task.md) work here too:
`prepare`, `answer`, `collect`, `mounts`, `context_query`.

Each task runs in a fresh workspace (`work/`, with the log in `log/` beside it). The agent submits its answer as
mini-swe-agent ends a task, `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && cat answer.txt`, and the submission is the
last message the judge scores; without one (the step limit, the time limit) it is the last assistant message. The
log holds the session's events (`agent-mini-swe-agent.jsonl`), which every Evolver reads as steps: the messages, each
command with its output and exit code, errors.

The model is a [backend reference](../guides/model-backends.md); mini-swe-agent calls it through litellm, at the
backend's URL with its key. The key is passed with each request and written nowhere; the agent's commands see `env`
and a few basic variables (`PATH`, `HOME`, `LANG`, ...) only, and on Linux mini-swe-agent's process is made
non-dumpable, so a command cannot read its environment either (on macOS a command run as you can, with `ps eww`).
That keeps the key out of mini-swe-agent's commands and process, not out of every process that holds it: with the
sandbox off, Hermit's own process (started with the key exported) runs as you too, as for every CLI agent. Only the
run's sandbox confines the commands (`sandbox.enabled`, Linux with bubblewrap): there the agent reaches its model and
nothing else.

Tokens are read from the responses and priced at the backend's `prices`; without a price the cost is unknown.
mini-swe-agent's own cost tracking is off, so its `cost_limit` never stops a task: `step_limit` and `timeout_s` do
(at `timeout_s` the process is stopped), and one request is bounded by 600 seconds. A failed model call (a context
overflow) fails the session, as a CLI's failed session does: no answer is judged, the task is recorded as an error, and
its tokens still count; a provider that could not serve (a rate limit, an outage, past mini-swe-agent's own retries)
runs the task again (`retries`). A setting that would fail every task (no model, a `config` that names the model, a
wrong key or model id found by the first call) starts no session and is reported as the agent's error, not as an
answer. At `timeout_s` the command that is running is stopped too.

`config` is a mini-swe-agent config file merged over Hermit's the way `mini -c` merges files: change
`agent.step_limit`, the templates, `model.model_kwargs` (temperature, `max_tokens`) or the `environment` section
(`timeout` of a command, more `env`). A system template of your own receives the adapter's block where it says
`{{ adapter_context }}`, or at its end. The model is the Base Agent's and the environment Hermit's local one: the
file may not set `model.model_name`, `model.model_class` or `environment.environment_class`. With the sandbox on,
declare the file in `environment.read`, as the task's own files are: the task sandbox shows nothing else of the run's
directory.

## 4. Run the example

`docs/_examples/mini_swe_agent/` has nine questions about a small CSV file and a judge that accepts exactly
`{"answer": <integer>}`, with GEPA as the learner (`--group gepa`). It uses DeepSeek for both:

```bash
python -m pip install -e ".[mini-swe-agent]" --group gepa
export DEEPSEEK_API_KEY=...
mkdir -p .demo && cp -R docs/_examples/mini_swe_agent .demo/mini-swe-agent
hermit check --config .demo/mini-swe-agent/run.yaml
hermit eval --config .demo/mini-swe-agent/run.yaml --adapter none
hermit train --config .demo/mini-swe-agent/run.yaml
hermit eval --config .demo/mini-swe-agent/run.yaml --adapter final
```

Another backend is one override, for example `model=openai/<model id>`. The tasks need the file and a few commands;
the format is what there is to learn, and a strong model may answer in it from the start. **There is no promised
score gain.** The example sets `sandbox.enforce: best_effort`: with bubblewrap each task runs in the task sandbox;
without it (macOS) the run is unconfined, recorded and warned.

## 5. Deploy to a mini-swe-agent workspace

```bash
hermit deploy --adapter .demo/mini-swe-agent/runs/mini-swe-agent/checkout --workspace .demo/mini-workspace \
  --host mini-swe-agent
cd .demo/mini-workspace
mini -c mini-swe-agent.yaml -m deepseek/deepseek-flash
```

The workspace holds `mini-swe-agent.yaml` (Hermit's prompts over mini-swe-agent's `mini` config, the adapter's block
written into the system template), the adapter's copy under `hermit/<id>/` and `.hermit-deploy.json`. An adapter with
tools also names Hermit's environment class and the adapter in the config's `environment` section, and every tool
call is appended to `tool-calls.jsonl`; run `mini` from the Python environment Hermit is installed in. The model and
the limits are mini-swe-agent's own settings (`-m`, its keys, global config and `cost_limit`), as for any `mini` run.
The deployment uses Hermit's prompts for the `mini` config; the acting agent's `model_class` and `config` are not
part of it: give `mini` those yourself (`-c mini-swe-agent.yaml -c agent/mini.yaml`), keeping its system template,
which holds the adapter's block. Redeploying replaces the config; the workspace must be empty or generated for the
same host.
