# AgentDyn

[AgentDyn](https://github.com/SaFo-Lab/AgentDyn) as a Hermit task: tool-using agents in AgentDyn's native Shopping,
GitHub and DailyLife environments, on **existing frozen** benign and injected cases, scored by AgentDyn's native
judges. Nothing here generates or optimizes attacks.

| Folder | Contents |
|---|---|
| [`utils/`](utils/) | The benchmark integration: the hook bridge to AgentDyn's agent loop and environments (`bridge.py`), the OpenAI-compatible victim (`victim.py`), the optional same-victim classifier (`auditor.py`), the native judge (`judge.py`), case preparation (`prepare*.py`), reports (`report.py`, `compare.py`), the domain specification (`task-info/`) and the data notes ([`DATA.md`](utils/DATA.md)) |
| [`evosafe/`](evosafe/) | Experiments with the [EvoSafeHarness](../../docs/methods/evosafe.md) method: run configs and how to run and read them |

The method itself lives in Hermit (`hermit/agent/evolver/methods/evosafe/`, template `evosafe@1.0`) and does not
depend on AgentDyn. `utils/` fills the method's
[task contract](../../docs/methods/evosafe.md#the-task-contract): the bridge is the task pipeline, the judge reports
defense verdicts, case rows carry `kind`, `group` and `domain`, and `task-info/` is the domain specification.

## Install

Use a separate Python 3.12 environment. AgentDyn installs as `agentdojo`; pin the tested revision.

```bash
git -C ../AgentDyn checkout 5353cf7615b135cace8d07c8f12dac53a16b6db3
uv venv .venv-agentdyn
uv pip install --python .venv-agentdyn/bin/python -e '.[test]' -e ../AgentDyn
source .venv-agentdyn/bin/activate
```

Install both projects in one resolver invocation, so AgentDyn's dependencies cannot change Hermit's pinned packages.

## Prepare the cases

```bash
cd examples/agentdyn/utils
python prepare_expanded.py /path/to/AgentDyn/runs/google_gemini-2.5-flash data/expanded-v2
python prepare_thirty.py data/expanded-v2 data/thirty-v1
python prepare_all_groups.py data/expanded-v2 data/thirty-v1 data/all-groups-v1
```

The importer keeps only suite, user task, injection task and frozen payloads; old traces and scores are discarded.
The result has 57 train, 54 validation and 54 test cases: every eligible user group with its clean case and two
existing injection goals, each group in one split. See [DATA.md](utils/DATA.md).

## The bridge and the hooks

`utils/bridge.py:run` runs one case: a fresh environment, AgentDyn's native agent loop, and the defense bundle's
hooks around every tool call.
- The victim is any OpenAI-compatible model, named by an `agent.model` with `agent.base: direct` backend reference such as
  `vllm/nvidia/llama-3.3-nemotron-super-49b-v1.5`. Endpoint and key settings come from Hermit's
  [model backends](../../docs/guides/model-backends.md), just as for the other task integrations. Legacy
  `base_url`/`api_key_env` settings remain supported. Requests retry on timeouts and server errors (`max_retries`).
- Every model call goes through the shared `direct` Base Agent and `ChatClient`, including the optional classifier.
  Native model names without backend connection settings are rejected.
- The episode fails, and never counts as secure, on any of these: a truncated response, a malformed tool call, a
  textual tool call, unfinished reasoning, or exhausting `max_iters`.

| Hook | Possible effect |
|---|---|
| `system_prompt_transform(prompt)` | Rewrite the system prompt. `ADAPTER.md` is appended before it runs. |
| `on_pre_tool_call(ctx)` | Rewrite the arguments, or refuse the call before it runs. |
| `on_post_tool_call(ctx, result)` | Rewrite or refuse the output. An effect that already happened is not undone. |

The hooks get the trusted user instruction and public tool schemas, never task IDs, injection labels, environment
state or judge predicates. The template's `.hermit/EVOLVER.md` and `utils/task-info/README.md` state the contract.
When `agent.params.auditor.enabled` is set, structured hooks also get `ctx.classify_output(text)`, a bounded,
side-effect-free classifier that uses the same frozen victim model.

## Scope

- Only indirect prompt injection: the user is benign and the attack arrives in tool outputs. AgentDyn has no
  direct-attack cases.
- Hook code runs in a separate networkless Docker container; native tools and judges remain in the trusted
  episode worker. Docker is required and failures never fall back to host execution. See
  [the execution boundary](evosafe/REPRODUCTION.md#execution-boundary).
