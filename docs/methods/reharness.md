# ReHarness

ReHarness (Xiao et al., 2026, [arXiv:2610.12668](https://arxiv.org/abs/2610.12668)) is a recurrent harness for
agentic continual learning. Its harness state is a bank of reflections, one per past instance of a stream. Before
each instance the harness retrieves the top-k reflections for the instance's input and gives them to the acting
agent; after it, the learner revises the retrieved reflections against the new trajectory and writes one more.
Choose it for an ordered stream whose instances recur in kind and that you want to learn strictly online: one pass,
no validation set, no replay, every learner call a chat completion through the run's `llm` tool.

## Setup

```yaml
template: reharness
evolver:
  name: reharness
  params:
    embedding: fireworks/accounts/fireworks/models/qwen3-embedding-8b   # the paper's retriever; null: BM25
llm: fireworks/accounts/fireworks/models/glm-5p3-flash   # the reflection and revision agents: the acting model
data:
  step_size: 1                   # one instance per step
  shuffle: false                 # the stream's order
trainer:
  epochs: 1                      # one pass
```

This fragment belongs in a complete [run config](../framework/configuration.md). The method needs no installation
extra: the template's code is standard library. What it needs from the run:

- `llm`, the reflection and revision agents (not with `raw_memory: true`, which calls no model).
- An acting agent that reads the adapter through `Session.context(query=<the instance's input>, task=<the row>)`
  and records `session.last_context` in `RunOutput.extra["context_tool"]`. The built-in `codex` and `claude-code`
  agents deploy `ADAPTER.md` alone and refuse an adapter with a context tool. The built-in `react` agent and the
  examples' agents do ([per-task context](../framework/adapter.md#per-task-context)).
- With `embedding` set, an OpenAI-compatible `/embeddings` endpoint, and `REHARNESS_EMBED_URL` in the environment
  for the acting side's reader; see [Embeddings and the relay](#embeddings-and-the-relay). With `embedding: null`
  nothing else.

## One framework step

For instance t with input x_t, trajectory tau_t = (x_t, y_t, r_t) and harness state h_t = {R_i}_{i<t}, the
paper's three steps are

```text
I_t     = Retrieval(x_t, h_t, k)                             (1)  forward read
y_t     = pi_D(x_t, {R_i}_{i in I_t})
R'_i    = pi_R(R_i, tau_i; tau_t)      for every i in I_t    (2)  backward revision
R_t     = pi_M(tau_t; {R'_i}_{i in I_t})                     (3)  forward write
h_{t+1} = h'_t (+) R_t
```

| Paper | Framework |
|---|---|
| Eq. 1, the forward read | On the acting side: the template's context tool `reflections`. The harness calls it once per instance with the instance's input and task row (`Session.context(query=x_t, task=row)`), and its `[RANKED REFLECTIONS]` block is appended below `ADAPTER.md` before the decision agent's first model call. The agent is never offered the tool. The pipeline records what was retrieved (`RunOutput.extra["context_tool"]`: ids, ranks, scores, the query), which the Trainer keeps as `pipeline_extra` in the learning material. |
| Eq. 2, the backward revision | The first half of the Evolver's `step`: for every reflection the pipeline recorded as retrieved, one call with the paper's RevisionAgent prompt, the reflection's own source trajectory (kept in `state/trajectories/`) as the sole source of content and tau_t as feedback. The calls run independently, `parallel` at a time; each revised text replaces the retrieved one in `reflections/<id>.json`, with a record of the revision. The rest of the bank is untouched. |
| Eq. 3, the forward write | The second half of `step`: one call with the ReflectionAgent prompt and the revised reflections as demonstrations. The new reflection, with the embedding of x_t, joins the bank as `reflections/<task id>.json`, and tau_t is stored for later revisions. |
| h_0 | `bootstrap`, recorded as step 0: `index.json` from the parameters (k, the retrieval, the embedding model and its dimensions), so the first instance already reads with them. A starting bank embedded with another model is re-embedded. |

One step is one instance (`data.step_size: 1`). When a pipeline records no context-tool result, the Evolver takes
the agent to have read no reflections: nothing is revised and the write gets no demonstrations, and the step's
report and record say so. An agent that reads `session.context()` without a query did receive `ADAPTER.md`
alone, so that is the truth of it; pass the instance's input as `query`. For a pipeline known to call the
reader but not to record `session.last_context`, `recompute_retrieval: true` ranks the bank the rollout read
again (the same ranking) and marks every revision it causes as recomputed.
The agents are the run's `llm` tool, metered by the Trainer. The paper uses one backbone for the decision,
reflection and revision agents; bind `llm` to the acting model.

## Parameters and defaults

```{literalinclude} ../../hermit/algorithms/evolvers/reharness/evolver.yaml
:language: yaml
```

## Key controls and their interactions

| Parameter | Values / default | Effect |
|---|---|---|
| `k` | Positive integer / 5 | Reflections retrieved per instance, and revised after it; written into `index.json` at bootstrap, where the reader takes it from |
| `embedding` | `<backend>/<model id>`, `{base_url, model, api_key_env, price}` or null / null | The retriever. Null selects BM25 over the instances' inputs and needs no endpoint. With a model, `REHARNESS_EMBED_URL` must name the reader's endpoint, a loopback URL (`127.0.0.1` or `localhost`) in a sandboxed run; `setup` refuses anything else. Every embedding in the bank must come from the model `index.json` names; the template's check rejects a bank that disagrees |
| `recompute_retrieval` | Boolean / false | When a pipeline recorded no context-tool result: false revises nothing for that instance (the agent read no reflections); true ranks the bank again as the reader would have and revises those, marking them recomputed |
| `revise` | Boolean / true | False is the paper's No Revision ablation: the forward flow only, retrieved reflections never change |
| `demonstrations` | Boolean / true | False writes each reflection without the revised reflections as demonstrations |
| `raw_memory` | Boolean / false | True stores the rendered trajectory itself in place of a reflection, with no model calls: the paper's Top-K Raw baseline on the same retrieval. The reader then renders `[RANKED TRAJECTORIES]`; give such a run an `ADAPTER.md` that describes the entries as trajectories (the template's text describes reflections) |
| `feedback` | Boolean / true | True puts the judge's score and feedback in tau_t's `[OUTCOME]` block, the environment's outcome (the paper's footing: online supervision). False keeps every verdict, score and reference answer away from the agents: the trajectory and the answer alone are their evidence, the label-free footing of a stream whose verdicts the learner must not see. The switch governs the `[OUTCOME]` block only: a venue whose own observations name the answer (CL-Bench database: `Correct answer: ...`) stays supervised through the trajectory. Recorded per instance in the state |
| `trace_chars`, `obs_chars` | Character limits / 24000, 4000 | Clip a trajectory shown to an agent (its middle is left out) and each message or observation inside it; the stored source trajectory keeps up to 400,000 characters |
| `max_tokens`, `temperature` | Per call / 8192, null | Sampling for the reflection and revision calls; null leaves the endpoint's default |
| `parallel` | Positive integer / 5 | Revision calls in flight at once; each retrieved reflection is revised independently of the others |

### The paper's protocol

- `data.step_size: 1`, `data.shuffle: false` and `trainer.epochs: 1`: one instance per step, in the stream's order,
  one pass. Several instances in one step are processed in order, each with its own retrieval and revision, and the
  step's state notes that the protocol was not the paper's.
- No validation data: the Evolver declares no needs and never reads `data.val`; there is no candidate gate and no
  replay. The final adapter is the bank after the last instance.
- The same backbone for the acting model and `llm`.
- k = 5; retrieval by Qwen3-Embedding-8B, query-to-query (the instance's input against each reflection's source
  input), over every past instance of the stream. The paper names no similarity metric; this integration ranks
  by cosine (the dot product of unit vectors).

## Embeddings and the relay

Two programs embed: the Evolver, when it writes a reflection, and the reader, when the harness retrieves for an
instance. Both call an OpenAI-compatible `/embeddings` endpoint with the model `index.json` names.

The Evolver reaches it through the `embedding` reference: the backend's base URL and key variable (Fireworks:
`https://api.fireworks.ai/inference/v1`, `FIREWORKS_API_KEY`). Without a price for the embedding model on the backend
or on the reference (`{model: fireworks/..., price: {input: ...}}`), its cost is recorded as unknown.

The reader is the adapter's tool, and inside a run the adapter's tools run in a strict sandbox: no keys, and only
the `127.0.0.1` endpoints whose URLs appear in the environment. So `REHARNESS_EMBED_URL` names a loopback relay in
front of the provider, and the relay signs the keyless requests:

```bash
PORT=3461 UPSTREAM=https://api.fireworks.ai UPSTREAM_KEY_ENV=FIREWORKS_API_KEY LEDGER=embed_ledger.json \
  python -m hermit.runtime.wire_meter &
export REHARNESS_EMBED_URL=http://127.0.0.1:3461/reharness/inference/v1
```

The relay listens on the loopback only, but every local process that reaches its port can spend the key through it:
on a shared machine, run inside the offline container (`docker/offline.sh`) or another private network namespace.

The examples' `launch.sh` start this relay (the CL-Bench ones beside their host and learner meters, the OfficeQA
one alone, its models calling the provider directly) and resolve the paths with `python -m hermit.backends shell
<ref>`. `setup` embeds one word through the Evolver's client before the first
step, so a wrong endpoint fails at startup rather than at the first instance. Outside a sandbox the reader can send
a key itself: `REHARNESS_EMBED_API_KEY_ENV` names the variable that holds it. `hermit eval` needs the same
variable: the final adapter retrieves for held-out instances too.

With `embedding: null`, `index.json` records `retrieval: bm25`, the reader ranks the bank's source inputs by Okapi
BM25, and no endpoint or variable is needed.

## Ablations and baselines

| Run | Setting |
|---|---|
| ReHarness | the defaults |
| No Revision | `revise: false` |
| No demonstrations | `demonstrations: false` |
| Top-K Raw | `raw_memory: true`: the same retrieval over stored trajectories, no reflection or revision agent; the block reads `[RANKED TRAJECTORIES]`, and `ADAPTER.md` should describe the entries so |
| No harness | `hermit eval --adapter none`, or the task's prompt-only configuration |

Hold the acting model, the stream order, the judge and the embedding model fixed across them. The ablations save
the k revision calls per instance; the step records show the difference.

## Where this integration departs from the paper

- **The reward.** r_t is the judge's score and feedback as the task's judge writes them. On OfficeQA the feedback
  names the reference answer, which the reflection and revision agents then read; `feedback: false` withholds it,
  and the `[OUTCOME]` block says so, for a stream run on the label-free footing of other methods.
- **The trajectory text.** tau_t is rendered from the pipeline's `messages`, or from its copied log when there are
  none (CL-Bench's turn records are rendered turn by turn; a Codex session's `agent-codex.jsonl` as its reasoning
  summaries, messages, commands with exit codes and output, file changes and tool calls), as `[TASK]`,
  `[TRAJECTORY]`, `[OUTPUT]` and `[OUTCOME]`. The system prompt, which holds the harness state itself, is left out.
- **The decision agent's prompt.** The acting pipeline owns its system prompt and action format, so pi_D's prompt is
  the venue's own; the template's `ADAPTER.md` carries the paper's framing of the retrieved memory above the
  `[RANKED REFLECTIONS]` block. The reflection and revision prompts are the paper's, verbatim
  (`hermit/algorithms/evolvers/reharness/prompts.py`), with the input laid out as they say.
- **The bank.** Retrieval ranks every reflection of the stream so far, one per past instance, whatever its kind. A
  memory id is the task id (`<task id>.s<step>` when a task recurs). The template starts with an empty bank; a seed
  adapter may carry one.
- **The agents' replies.** Each agent is asked for one JSON field. The reply is read tolerantly: the last object
  that holds the field when a draft precedes the final one or reasoning leaked into the reply, the field alone when
  the object does not parse (a bare quote or a markdown escape inside the string). A reply without the field gets
  one reminder; a reply cut off at `max_tokens` is asked for again with twice the budget; when the second attempt
  fails too, the reply's text is kept rather than nothing, unless it was cut off or is a broken JSON wrapper, in
  which case nothing is written for that instance. The state records which happened for every call.

## What to expect

Per instance: one reflection call, one revision call per retrieved reflection (k once the bank holds k), and one
embedding call for x_t (one more when the retrieval had to be recomputed). The reader's own embedding call goes
through the relay and appears in its ledger, not in the run's usage. The bank grows by one file per instance, about
11 KB of base64 embedding each at 4096 dimensions (8 KB of half floats), versioned in the adapter's Git tree.

The first instance reads an empty bank and gets the static context alone; retrieval helps only once instances of the
same kind have recurred. A revision can leave a reflection unchanged (indel distance 0), shorten it or lengthen it.
A pipeline error that leaves no trajectory skips the instance; an `llm` error is recorded and the instance goes on
without a reflection. In the CL-Bench hosts a reader that fails (the embedding relay unreachable past the
client's retries) leaves the instance `ADAPTER.md` alone and records `retrieval: error: ...` in `context_tool`, so
nothing is revised for it and the stream goes on; the reported runs met no such instance. Scores in a step record describe the adapter before that step's update.

For planning, the CL-Bench database runs with GLM 5.3 Flash (reasoning effort high; 40 questions per stream): a step
took about a minute, about 40 seconds of it the learner's five revisions and one reflection side by side, at about
USD 0.011 per step at the backend's prices; reflections grew under revision to about 7 K characters each on
average after the stream (the longest 21 K), so the five retrieved ones add about 35 K characters to the decision
agent's context on average over the stream (38 K over its last quarter). About 3 % of the agents' replies were not
the JSON asked for at the first attempt.

### Records to read after a run

| Path | Contents |
|---|---|
| `step_NNNN/output/report.md` | Per instance: what was retrieved, which reflections changed and by how much, the memory written |
| `step_NNNN/state/reharness.json` | One record per instance: the retrieved ids with ranks and scores, and whether they came from the pipeline's record or were recomputed; each revision's size as the indel distance (characters inserted plus deleted) with the lengths before and after; the write's calls, finish reason and how the reply parsed; the bank size |
| `step_NNNN/state/trajectories/<memory id>.txt` | tau_i as a later revision of R_i reads it |
| `checkout/reflections/<id>.json` | A reflection with its source input, embedding and `revisions`: the step, the instance that triggered each, the rank it was retrieved at, the size of the change |
| `checkout/index.json` | k, the retrieval, the embedding model and dimensions |
| `steps.jsonl` | Each step's status and usage; the embedding model's calls are their own entry |

Evaluate the final adapter against `root` and `none` on the held-out split with the
[same protocol](../guides/evaluation.md); for a benchmark stream, keep its own scoring
([Ordered streams](../guides/evaluation.md#ordered-streams)).

## Examples

`examples/clbench_database/reharness/`, `examples/clbench_poker/reharness/` and `examples/officeqa/reharness/` run
this method on the three tasks; their `launch.sh` start the embedding relay (the CL-Bench ones beside the host and
learner meters), set the reader's variable and bind the models that go through a meter. On OfficeQA the Codex configs (`stream_codex.yaml`, `run_codex_flash.yaml`)
act through `utils/agent/codex_agent.py`, which deploys a context adapter as the session's `AGENTS.md`: the
Session's context for the question, `ADAPTER.md` and the retrieved reflections below it, retrieved once before
the session starts. The [CL-Bench](../recipes/clbench.md) and [OfficeQA](../recipes/officeqa.md) recipes describe the tasks'
data, models and scoring.
