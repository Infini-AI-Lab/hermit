# First training with a real model

Use an API endpoint to run both an acting model and the [GEPA](../methods/gepa.md) learner on nine included
sentiment tasks. No external dataset, GPU, or agent CLI is needed; GEPA's library is an installation extra. Unlike
the [offline walkthrough](quickstart.md), both acting and learning make real model calls.

This is an integration smoke test: three training tasks, three validation tasks with which GEPA chooses between
candidate adapters, and three held-out test tasks. **There is no promised score gain.** A strong model may already
satisfy the judge, and a learned update can also perform worse.

## 1. Install and make a working copy

Complete [Installation](installation.md), activate its environment, then run from the repository root:

```bash
python -m pip install -e . -r requirements-gepa.txt
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
hermit check --config .demo/first-model/run.yaml --no-probe
hermit check --config .demo/first-model/run.yaml
hermit eval --config .demo/first-model/run.yaml --adapter none
```

The first command loads components without an acting-model call. It does not authenticate the endpoint or prove
that the learner works. The second sends one short request to the learner endpoint and runs one acting task;
`eval` runs all three held-out tasks.

The judge requires exactly `{"label": "positive"}`, `{"label": "negative"}`, or `{"label": "neutral"}`.
Invalid JSON, extra keys, prose, and wrong labels score zero. A low score without execution errors is valid
feedback; connection or model errors require investigation.

## 4. Train and evaluate the selected adapter

```bash
hermit train --config .demo/first-model/run.yaml
hermit eval --config .demo/first-model/run.yaml --adapter root
hermit eval --config .demo/first-model/run.yaml --adapter final
```

One batch of three training attempts becomes learning material. GEPA first scores the starting adapter on the
three validation tasks. If a training attempt failed, it asks the learner model for a new `ADAPTER.md` from the
attempts and the judge's feedback, runs that version on the same three training tasks, and keeps it only if it scores
higher; a kept version is then scored on the validation tasks. If all three training attempts were already correct,
there is nothing to learn from and the step proposes nothing. The Trainer checks and records the result. The
initial template is `root`; `final` is the candidate with the best validation score, which is `root` when no new
version was kept.

The complete sequence makes **16 to 22 acting requests**: one probe, three no-adapter tests, three training
attempts, three validation tests of the starting adapter, three tests of a proposed version on the training tasks and
three validation tests if it is kept, three root tests, and three final tests. There are **one to three learner
requests**: the probe from `check`, then none during training when every training attempt was correct, otherwise
one, with one more if the reply holds no usable instructions. HTTP retries can add provider requests.

```{raw} html
:file: ../_includes/first-model-walkthrough.html
```

See more about training at [Evolving an adapter](../framework/overview.md#evolving-an-adapter).

The YAML uses token and time budgets; prices are not invented, so dollar cost is reported as unknown. Add
verified `llm.price` settings for learner dollars and acting-client prices in the copied pipeline if you need
dollar accounting. Probe and test calls are outside the training budget; GEPA's candidate tests during training
count toward it. Training can overshoot during in-flight requests; use endpoint-side spending controls when needed.

## 5. Inspect what was learned

```bash
cat .demo/first-model/runs/first-model/checkout/ADAPTER.md
cat .demo/first-model/runs/first-model/steps.jsonl
cat .demo/first-model/runs/first-model/eval/final/summary.json
cat .demo/first-model/runs/first-model/step_0001/output/report.md
cat .demo/first-model/runs/first-model/step_0001/state/gepa.json
```

`report.md` says what GEPA did with the step: kept a new version, rejected it, or skipped. `gepa.json` holds the
candidate pool with each candidate's validation scores.

Look for a general classification/formatting rule, not copied training reviews. The feedback explicitly discloses
training labels in this protocol, and the learner model sees only the training attempts and their feedback. GEPA's
code receives the validation results and keeps only their scores; the feedback of a wrong validation answer names its
label, and the run's `tools.jsonl` records it. Test labels stay in judge-owned code. This in-process,
sandbox-disabled example does not create a security boundary around those files.

Require `errors: 0` when comparing scores. `mean_score` is the fraction of correct task attempts here.
Three test tasks do not establish a reliable improvement. The final adapter can be deployed using
[the deployment tutorial](../tutorials/deploy-an-adapter.md).

For another completed experiment, use a new `trainer.run_name` on every related command.
To change the task or judge, continue with [Bring your task](../tutorials/your-own-task.md). For larger model
experiments, use the [recipe catalog](../recipes/index.md).
