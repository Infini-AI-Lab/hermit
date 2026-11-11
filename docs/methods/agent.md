# Codex, Claude, Pi, OpenCode and DeepSeek Harness agent learners

Use `evolver.name: codex`, `claude`, `pi`, `opencode` or `dsh` when a learner should inspect trajectories, use tools,
and edit an adapter directory over several actions. This is useful for skills, multiple files, and tool code that a
single text rewrite does not capture well. The `pi` learner signs in through a model reference (`evolver.base: pi` with
`evolver.model: <backend>/<model>`, or `evolver.params.model`); it copies no login from `~/.pi/agent` (`home: []`).
The `opencode` learner runs only a model reference the same way (`evolver.base: opencode`); without one its session
is refused.
The `dsh` learner signs in as the `pi` learner does (`evolver.base: dsh`) and copies no file of `~/.dsh` (`home: []`).
On the single-file prompt-only template, the Codex learner is the [prompt-only](prompt-only.md) method.

## Every method on Pi

`evolver.base: pi` with `evolver.model: <backend>/<model>` runs any method's learner on Pi, on that model:

| Method | What runs on Pi |
|---|---|
| `pi`, Meta-Harness Adapter, ASPIRE, MWM, AHE Adapter, Harness², RRSI Adapter, EvoSafe | The method's own sessions, as on Codex or Claude Code |
| GEPA, SkillOpt, ACE, OPRO, ReHarness, Continual Harness | They call the run's model, so that model becomes Pi: each call is one session with no tools (the same as `llm: {cli: pi, model: <backend>/<model>}`) |

Pi signs in only through the model reference and has no web search. Where the backend speaks an OpenAI-compatible API
(chat completions or the Responses API), a request's `temperature` and `top_p` reach the model as Pi's
`samplingParams`; on the Messages API Pi ignores them, so there a request that asks for a `temperature` is refused
unless `evolver.pi.unapplied` names it. The backend's `extra_body` goes with each chat-completions request. A session's
`effort` is what the model is sent, over an `extra_body.reasoning_effort`; on the Messages API Pi gives `xhigh` and `max`
the same thinking budget as `high`, so there they are refused. Pi has no sub-agents: an adapter's sub-agents stay
files its acting agent can read. Pi's `grep` and `find` tools run `rg` and `fd`, which an offline Pi cannot fetch: RRSI's
digesters get them only where the machine has them. Pi caps one reply at 16384 tokens unless the backend says otherwise:
a reply cut there comes back with `finish_reason: length`, or fails the session that needed it whole. Where a long answer
is due (EvoSafe's whole files) and the model reasons at length, give the backend a larger
[`max_output_tokens`](../guides/model-backends.md). Pi's `bash` has no time limit of its own: a command the model
gives none stops after 600 seconds, so one command that never ends costs that much of a step, not the whole call.
`command_timeout_s` in `evolver.pi` (the learner's sessions) or `agent.pi` (the acting agent's) changes it; `0` turns it
off.

## Requirements and setup

Install and authenticate the selected learner CLI in the environment where training runs. Sandboxed program
execution uses Linux and bubblewrap. The acting pipeline can use a different model or host.

```yaml
evolver:
  name: codex
  resume: false
sandbox:
  enabled: true                  # the learner works in /w; its model's key and host are passed in
environment:
  read: [../utils/agent]         # what the acting agent's sandbox shows: its code, never the data or the judge
```

Complete the endpoint grants, environment, and task resources for your learner; the fragment alone is not a
working model configuration. Use [Installation](../get-started/installation.md) and
[configuration boundaries](../framework/configuration.md#sandboxes).

## Update contract

The learner edits the working adapter using the step's learning material and `.hermit/EVOLVER.md`. The Trainer owns
acceptance and Git history. Files outside the learnable rules are rejected or repaired. For a performance gate,
the method must explicitly evaluate and select candidates; file checks alone do not provide one.

Persistent learner state, host conversation resume, and Trainer crash recovery are separate mechanisms.
Only enable `evolver.resume` for a backend that declares support; `check` reports unsupported requests.

## Defaults

Codex:

```{literalinclude} ../../hermit/agent/evolver/methods/codex/evolver.yaml
:language: yaml
```

Claude:

```{literalinclude} ../../hermit/agent/evolver/methods/claude/evolver.yaml
:language: yaml
```

`prompt` and `resume_prompt` name learner instructions; `args` passes CLI arguments. Inspect the built-in
definition before replacing prompts. For a custom learner harness, use
[AgentEvolver or CommandEvolver](../framework/custom-evolver.md#agent-and-command-implementations).

Report usage outside the framework's metered tools so budget records remain meaningful. A learner's direct
external calls are not automatically accounted for merely because the Trainer launched it.

## Learner tool access

Agent-backed learners can set `evolver.params.tools` to an allowlist such as `[check_spec]`; null exposes all
Trainer tools. The session's tool server (started by the Base Agent for the call) serves only the allowed ones, and
the task message then names no Trainer endpoint. Inspect the selected method's defaults and
`hermit/agent/evolver/agent.py`. This controls the learner's Trainer tools, separately from the acting agent's
task/adapter tools. It does not make arbitrary shell commands safe or impose a mandatory check order.

For a complete task-specific Codex configuration, see `examples/clbench_poker/prompt_only/run.yaml`. Acting agents are configured under `agent`; `evolver.name: codex` selects the
learner, not the acting host.
