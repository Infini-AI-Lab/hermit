# Model backends

Every model a run calls is named by a **reference**, `<backend>/<model id>`, and a **backend** is one endpoint:
its base URL, the environment variable holding its key, the API dialects it serves and, optionally, prices per
model. A config names models by reference; which endpoint answers is decided in one place, so the same run can be
pointed at Fireworks, at OpenAI or DeepSeek, or at a vLLM or SGLang server on your own machine without editing the
config, the launchers or the task agents.

```yaml
model: fireworks/accounts/fireworks/models/glm-5p3-flash   # the run's model: the `llm` tool, the agent and the Evolver
                                                            # use it when they name none of their own
```

```bash
harnessadapter backends list                 # the backends known here
harnessadapter backends show vllm/Qwen/Qwen3-30B-A3B-Instruct-2507-FP8        # a reference, resolved
harnessadapter backends check vllm/Qwen/Qwen3-30B-A3B-Instruct-2507-FP8       # does the server answer each dialect?
harnessadapter train --config run.yaml model=vllm/Qwen/Qwen3-30B-A3B-Instruct-2507-FP8   # the same run, locally
```

## Where a model is named

| Setting | Who calls the model | Dialect it speaks |
|---|---|---|
| `llm` | the run's `llm` tool: the `prompt_only`, `gepa` and `skillopt` Evolvers | chat completions |
| `agent.params.model` with `agent.name: react` | the built-in tool-calling agent | chat completions |
| `agent.params.model` with `agent.name: codex` / `claude-code` | the CLI acting agent | Responses API / Messages API |
| `evolver.params.model` of the `codex`, `claude` and `mwm` Evolvers | the learner's CLI session | Responses API / Messages API |
| `model` (top level) | the default for all of the above when they name none | |
| the examples' task agents (`agent.params.model`, or the launchers' `MW_MODEL`) | CL-Bench's litellm host, ServeLearnBench's loop, the SkillOpt rollout | chat completions |

Each accepts a reference string, or a mapping `{model: <reference>, price: {...}, extra_body: {...}}` when a price
or request fields are to be set for this use only. The older mapping `{base_url, model, api_key_env, price,
extra_body}` still works for the chat-completions callers and names the endpoint directly.

A reference's first segment is the backend; the rest is the model's id on it, with whatever slashes the backend
uses (`fireworks/accounts/fireworks/models/glm-5p3-flash`, `vllm/Qwen/Qwen3-30B-A3B-Instruct-2507-FP8`). A
backend may define aliases (`models: {qwen: Qwen/Qwen3-30B-A3B-Instruct-2507-FP8}`), so `vllm/qwen` works too.
A string whose first segment is no backend's name is not a reference: for a Codex or Claude Code agent it is a
model of the CLI's own provider (`gpt-5.4`), as before. So a provider id that happens to start with a backend's
name (`openai/...`) is read as a reference to that backend.

The top-level `model` is a default: `model=vllm/qwen` on the command line moves every caller that names no model
of its own. The OfficeQA configs name none (one `model:` line serves the react agent and the `llm` tool), so one
override moves the whole run; a config that sets `llm` or `agent.params.model` itself keeps them unless they are
overridden too (`llm=vllm/qwen agent.params.model=vllm/qwen`). The `codex_glm` Evolver pins its model in its
manifest; `evolver.params.model=vllm/qwen` moves it.

## Where backends are declared

Three layers, each overriding the one below by name:

1. **the run config's `backends:` section**, or `backends.<name>.<field>=...` on the command line;
2. **your own file**, `~/.config/harnessadapter/backends.yaml` (or the file `HA_BACKENDS` names): the place for
   the servers you run, declared once for every example and run;
3. **the framework's built-ins**, `harnessadapter/backends.yaml`: `fireworks`, `openai`, `anthropic`, `deepseek`,
   `vllm` (`http://127.0.0.1:8000/v1`), `sglang` (`http://127.0.0.1:30000/v1`) and `ollama`.

`<NAME>_BASE_URL` in the environment moves a backend without a file: `VLLM_BASE_URL=http://gpu-box:8000/v1`. The
key is read from the backend's `api_key_env` (`<NAME>_API_KEY` by default: `FIREWORKS_API_KEY`, `VLLM_API_KEY`); a
local backend has `key_required: false`, so an unset variable means no key is sent.

```yaml
# ~/.config/harnessadapter/backends.yaml
vllm:                                   # the built-in, moved: every other field stays
  base_url: http://gpu-box:8000/v1
  api_key_env: LAB_VLLM_KEY
  models: {qwen: Qwen/Qwen3-30B-A3B-Instruct-2507-FP8}
  prices: {Qwen/Qwen3-30B-A3B-Instruct-2507-FP8: {input: 0, cached_input: 0, output: 0}}   # a bill of zero, not unknown
lab-sglang:                             # a new one, like the built-in sglang at another address
  from: sglang
  base_url: http://gpu-box:30000/v1
```

| Field | Meaning |
|---|---|
| `base_url` | the OpenAI-compatible root, normally ending in `/v1`; chat completions and the Responses API are under it |
| `api_key_env` | the environment variable holding the key (default `<NAME>_API_KEY`) |
| `key_required` | `false` for a server that needs no key (default `true`) |
| `apis` | the dialects served: `chat`, `responses`, `messages` (see below) |
| `messages_url` | the root the Anthropic Messages API is under, when it is not `base_url` without its `/v1` |
| `litellm` | the litellm route prefix, for task agents that call through litellm (`fireworks_ai`, `hosted_vllm`) |
| `prices` | per model id, `{input, cached_input, output}` in USD per million tokens; a model without one has an unknown cost (never zero) |
| `extra_body` | fields added to every chat request, such as `reasoning_effort` |
| `context_window` | tokens; Codex is told it as `model_context_window`, Claude Code as `CLAUDE_CODE_MAX_CONTEXT_TOKENS` |
| `developer_role` | `keep` (default), or `system`: a relay in front of the backend renames the `developer` role Codex uses, for an endpoint that refuses it (the built-in `fireworks` sets it, as the examples always did; Qwen3.6's chat template refuses a `system` message after the first, so a local server keeps the role) |
| `models` | aliases: short names for model ids |
| `from` | start from a copy of another backend (the examples put a metering relay in front of one this way) |

## Dialects: what Codex and Claude Code need

The framework's own client, the react agent and the examples' task agents speak **chat completions**. The CLI
agents do not: **Codex speaks the OpenAI Responses API only** (`wire_api = "chat"` is rejected since Codex 0.95),
and **Claude Code speaks the Anthropic Messages API only**. A backend's `apis` says what it serves, and the
framework checks a model's dialect against its backend before a run starts: `harnessadapter check` reports it,
`train` refuses to start on a mismatch, and `harnessadapter backends check <ref>` sends one short request in each
dialect (and lists `GET /models`) so a wrong claim shows up before anything is spent.

| Backend | chat | responses (Codex) | messages (Claude Code) |
|---|---|---|---|
| `fireworks` | ✓ | ✓ | ✓ (under `/inference`) |
| `openai` | ✓ | ✓ | |
| `anthropic` | | | ✓ |
| `deepseek` | ✓ | ✓ | ✓ (under `/anthropic`) |
| `vllm` (0.11.1 or later for `/v1/messages`) | ✓ | ✓ | ✓ |
| `sglang` | ✓ | ✓ | ✓ |
| `ollama` (0.14 or later) | ✓ | ✓ | ✓ |

A backend that serves chat completions only can still run the framework's own calls and the benchmarks' agents,
but not a Codex or Claude Code acting agent or Evolver; put a translating proxy (LiteLLM serves `/v1/responses` and
`/v1/messages` in front of any chat endpoint) between them, declared as its own backend.

Serving the Responses API is not quite enough for Codex. Codex (0.162) sends its sub-agent tools and every MCP
server's tools as `namespace` tools in each request, and the framework's learners always have an MCP server. The
`responses` probe of `check` sends one, so a server that refuses it is reported before a run. What was observed
on this framework's test machine (2026-10):

| Server and model | Codex learner |
|---|---|
| vLLM 0.31, Qwen3 / Qwen3.6 (its standard Responses path flattens namespaces) | works |
| vLLM 0.31, gpt-oss (its Harmony path) | every request refused: `tool type 'namespace' is not supported` |
| SGLang 0.5.22, gpt-oss | accepted, but the tools inside a namespace are dropped (the model cannot call MCP tools) and usage is reported as zero |

Pick a model the standard path serves for a Codex learner on a local server. On that path vLLM shows a namespaced
tool to the model as `<namespace>__<name>` (`mcp__evolver_tools__check_spec`) and maps only that name back; a call
by the bare name (`check_spec`) reaches Codex without its namespace and fails as `unsupported call`. Qwen3-30B used
the flat names. Qwen3.6 used the bare name that the Codex Evolver's prompt gives (`Call the check_spec tool`): in a
full CL-Bench run its learner sessions all completed, since the Trainer checks every step's adapter itself, but none
of its `check_spec` calls went through. A model must also be able to carry a long agent session:
Qwen3-30B-A3B-Instruct-2507 completed the prompt-only learner's sessions but looped through a MWM bootstrap session
without writing anything.

What the binding gives each CLI:

- **Codex**: `-c model_provider=<backend> -c model_providers.<backend>.base_url=... .wire_api="responses"` and
  `.env_key=<api_key_env>` when there is a key (Codex sends no credential otherwise), `model_context_window` when
  the backend declares one, and `web_search="disabled"` off OpenAI. These go in front of `evolver.params.args` or
  `agent.params.args`, so your own `-c` settings win.
- **Claude Code**: `ANTHROPIC_BASE_URL` (the backend's Messages root), `ANTHROPIC_AUTH_TOKEN` (the key as a bearer
  token, which is what vLLM and SGLang check; a placeholder for a keyless server, since Claude Code asks for a login
  without one), `ANTHROPIC_MODEL` and every model alias (`ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU,FABLE}_MODEL`,
  `CLAUDE_CODE_SUBAGENT_MODEL`) set to the model, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` off Anthropic, and
  `CLAUDE_CODE_MAX_CONTEXT_TOKENS` when the backend declares a `context_window`: for a model it does not know, Claude
  Code assumes 200k tokens and compacts too late for a smaller server (a resumed Evolver session on a 131,072-token
  vLLM server asked for 99k prompt plus 32k output tokens and failed).
  Claude Code 2.1.302 took model ids with `/` in them (`Qwen/Qwen3-4B-Instruct-2507` on vLLM,
  `accounts/fireworks/models/...` on Fireworks) in print mode; vLLM's own guide still recommends a plain served
  name (`vllm serve ... --served-model-name qwen3`).

The Evolver's own endpoint and key variable always reach the sandbox: the Trainer adds the binding's URLs to
`sandbox.allow` and its `api_key_env` to `sandbox.env`, so a config lists neither. Claude Code's token is written
into the Evolver's settings as `$<api_key_env>` and expanded when the agent starts, so the run's `config.yaml`
records the variable's name, never the key.

## A local server

```bash
# vLLM: tool calling needs the model's parser; a plain served name suits Claude Code
vllm serve Qwen/Qwen3-30B-A3B-Instruct-2507-FP8 --port 8000 --tensor-parallel-size 2 \
    --enable-auto-tool-choice --tool-call-parser hermes --served-model-name qwen3 [--api-key ...]
# SGLang: Claude Code's requests carry a `thinking` field, which SGLang's /v1/messages takes only with a reasoning
# parser, and ask for 32k output tokens, so the context length must leave room for them
python -m sglang.launch_server --model-path Qwen/Qwen3-30B-A3B-Instruct-2507-FP8 --port 30000 \
    --tool-call-parser qwen25 --reasoning-parser qwen3 --context-length 131072 --served-model-name qwen3
```

```bash
harnessadapter backends check vllm/qwen3                    # /models, then chat, responses and messages
harnessadapter check --config examples/officeqa/prompt_only/run.yaml model=vllm/qwen3
harnessadapter train --config examples/officeqa/prompt_only/run.yaml model=vllm/qwen3
```

The examples' launchers take the same references: `MW_MODEL=vllm/qwen3 VLLM_BASE_URL=http://127.0.0.1:8000/v1`
for both the host and the learner, `MW_HOST_MODEL` / `MW_EVOLVER_MODEL` for either alone
([CL-Bench](../recipes/clbench.md), [ServeLearnBench](../recipes/servelearnbench.md)); `--model vllm/qwen3` for
the Lean swarm (`examples/lean_swarm/launch.sh`). Their wire meters sit in front of whichever backend the
reference names: `python -m harnessadapter.backends shell <ref>` prints a backend's origin and path for the meter,
and a `backends.learner={from: <backend>, base_url: <the meter's URL>}` override binds the learner through it.

Prices: a hosted backend's models have prices in the built-in table; a local model has none, so its cost is
recorded as unknown. Give it a price of zero in your backends file when a bill of zero is what you mean. The
wire-level token counts are recorded regardless. Claude Code reports a cost of its own, at Anthropic's prices; for a
model on another backend that figure is replaced by the backend's price (unknown without one).

Context length: a local server is often started with a shorter context than the hosted model has, and a request
whose prompt plus `max_tokens` exceeds it fails (vLLM: HTTP 400, "maximum context length"). The built-in `react`
agent keeps its whole history, so a task whose tool results pile up fails on it: on OfficeQA, one question's history
on Qwen3.6-35B-A3B reached 114,689 tokens, and with `max_tokens: 16384` the request exceeded a 131,072-token server
by one token. Serve the model with its full context (vLLM `--max-model-len`, SGLang `--context-length`), or lower
`agent.params.max_tokens` and `tool_result_chars`.
