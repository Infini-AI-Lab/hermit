# First training with a real model

Use an API endpoint to run both an acting model and the built-in prompt-only learner on six included sentiment
tasks. No external dataset, benchmark package, GPU, or agent CLI is needed. Unlike the
[offline walkthrough](quickstart.md), both acting and learning make real model calls.

This is an integration smoke test: three training tasks and three held-out tasks. **There is no promised score
gain.** A strong model may already satisfy the judge, and a learned update can also perform worse.

## 1. Install and make a working copy

Complete [Installation](installation.md), activate its environment, then run from the repository root:

```bash
mkdir -p .demo
cp -R docs/_examples/first_model .demo/first-model
```

All generated runs and edits stay in the copy. Use a fresh destination if you already completed it.

## 2. Configure the acting and learner models

Use an endpoint that accepts chat-completions requests with system/user messages, `max_tokens`, `temperature`,
text responses, and token usage. The client appends `/chat/completions` to the base URL.

```bash
export MODEL_BASE_URL='https://your-endpoint.example/v1'
export MODEL_NAME='your-model-id'
read -r -s MODEL_API_KEY
export MODEL_API_KEY
python .demo/first-model/configure.py
```

Replace the two placeholders. The `read` command waits for you to paste the key and press Enter without displaying
it. The configuration helper writes endpoint and learner-model names into the copied YAML, never the credential.

By default both sides use `MODEL_NAME`. Set `ACTOR_MODEL` and/or `LEARNER_MODEL` before configuring to choose
different models. Optionally set `ACTOR_BASE_URL` / `ACTOR_API_KEY` and
`LEARNER_BASE_URL` / `LEARNER_API_KEY` for separate endpoints and credentials; unset values fall back to the shared
settings above. Re-run the helper after changing learner settings. Keep acting settings fixed across baseline,
training, and evaluation.

| Side | Source of settings |
|---|---|
| Acting pipeline | Runtime environment; actor-specific endpoint/model/key override shared settings |
| Learner | Copied YAML's `llm` section, written by the helper; credential variable name is saved, its value is not |

If your model needs different request fields or a larger reasoning-token allowance, adjust the copied
`pipeline.py` and `evolver.params` before running. The example's `max_tokens=256` actor setting is for a short
text response; some reasoning models need more.

## 3. Check and measure the baseline

```bash
harnessadapter check --config .demo/first-model/run.yaml --no-probe
harnessadapter check --config .demo/first-model/run.yaml
harnessadapter eval --config .demo/first-model/run.yaml --adapter none
```

The first command loads components without an acting-model call. It does not authenticate the endpoint or prove
that the learner works. The second runs one acting task; `eval` runs all three held-out tasks.

The judge requires exactly `{"label": "positive"}`, `{"label": "negative"}`, or `{"label": "neutral"}`.
Invalid JSON, extra keys, prose, and wrong labels score zero. A low score without execution errors is valid
feedback; connection or model errors require investigation.

## 4. Train and evaluate the selected adapter

```bash
harnessadapter train --config .demo/first-model/run.yaml
harnessadapter eval --config .demo/first-model/run.yaml --adapter root
harnessadapter eval --config .demo/first-model/run.yaml --adapter final
```

One batch of three training attempts becomes learning material. The prompt-only learner rewrites `ADAPTER.md`,
and the Trainer checks and records the candidate. An unusable learner reply can leave the adapter unchanged.
The initial template is `root`; `final` is the selected result.

The complete sequence normally makes **13 acting requests**: one probe, three no-adapter tests, three training
attempts, three root tests, and three final tests. There is one learner rewrite request for the default single file, with one
additional request if the response contains no usable file. HTTP retries can add provider requests. The normal total of 13 acting plus one learner generation assumes every command succeeds once and the learner reply has a usable file.

The YAML uses token and time budgets; prices are not invented, so dollar cost is reported as unknown. Add
verified `llm.price` settings for learner dollars and acting-client prices in the copied pipeline if you need
dollar accounting. Probe and test calls are outside the training budget. Training can overshoot during in-flight
requests; use endpoint-side spending controls when needed.

## 5. Inspect what was learned

```bash
cat .demo/first-model/runs/first-model/checkout/ADAPTER.md
cat .demo/first-model/runs/first-model/steps.jsonl
cat .demo/first-model/runs/first-model/eval/final/summary.json
cat .demo/first-model/runs/first-model/step_0001/output/report.md
```

Look for a general classification/formatting rule, not copied training reviews. The feedback explicitly discloses
training labels in this protocol. Test labels stay in judge-owned code and are not sent as task input to the
learner; this in-process, sandbox-disabled example does not create a security boundary around those files.

Require `errors: 0` when comparing scores. `mean_score` is the fraction of correct task attempts here.
Three test tasks do not establish a reliable improvement. The final adapter can be deployed using
[the deployment tutorial](../tutorials/deploy-an-adapter.md).

For another completed experiment, use a new `trainer.run_name` on every related command.
To change the task or judge, continue with [Bring your task](../tutorials/your-own-task.md). For larger model
experiments, use the [recipe catalog](../recipes/index.md).
