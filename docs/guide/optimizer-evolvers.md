# Optimizer Evolvers: GEPA, SkillOpt, and your own

MetaHarnessAdapter separates three choices that published methods usually
bundle together:

| Choice | Examples | Where it lives |
| --- | --- | --- |
| Architecture: what the adapter can contain | `prompt-only`, `mwm`, `lemma-board` | `harnessadapter/architectures/` |
| Evolver: how an adapter changes | learners (`basic`, `mwm`); optimizers (`gepa`, `skillopt`) | `harnessadapter/evolvers/`, or any directory |
| Environment: how an adapter is scored | your benchmark's harness and grader (e.g. OfficeQA Pro with GLM in native Codex) | `tasks/<venue>/`, or any `module:factory` |

Any Evolver can be paired with any architecture it supports and scored by any
Environment. Every candidate deploys the same way (context block, optional MCP
tools, optional native roles), and `harnessadapter evaluate` scores every
candidate the same way, so a comparison between Evolvers changes only the
Evolver.

## Two kinds of Evolver, one command

`harnessadapter evolve` runs both kinds. It reads `kind` in the Evolver's
`evolver.toml` and refuses options that belong to the other kind.

- **Learner Evolvers** (`basic`, `mwm`, ...) are prompts for a sandboxed Codex
  or Claude Code session that reads LearningMaterial and edits the candidate.
  See [Evolution](evolution.md).
- **Optimizer Evolvers** (`kind = "optimizer"`) are search plugins. They propose
  adapter texts, score them in an Environment, and keep what scores better.
  GEPA and SkillOpt are bundled. Each runs its upstream library unchanged; its
  `plugin.py` supplies only the object that library asks a new system for.

| | GEPA ([gepa-ai/gepa](https://github.com/gepa-ai/gepa), ICLR 2026) | SkillOpt ([microsoft/SkillOpt](https://github.com/microsoft/SkillOpt), 2026) |
| --- | --- | --- |
| Upstream entry point | `gepa.optimize()` | `ReflACTTrainer(cfg, env_adapter).train()` |
| What `plugin.py` supplies | a `GEPAAdapter` (`evaluate`, `make_reflective_dataset`) | an `EnvAdapter` (`rollout`, split loader) |
| Candidate | `{relative path: text}` over the adapter's text files | one document of the adapter (its context file) |
| Search | reflective mutation, per-instance Pareto front over `val`, minibatch acceptance | bounded add/delete/replace edits, textual learning rate, strict `val` gate, rejected-edit buffer, slow update, meta skill |
| Training State | GEPA's `run_dir` (`gepa_state.bin`) | SkillOpt's `out_root` (`runtime_state.json`, `history.json`) |

## Running

```sh
pip install -e '.[gepa]'                       # GEPA core has no dependencies
pip install -e /path/to/SkillOpt               # SkillOpt's recipe lives in its source tree
export FIREWORKS_API_KEY=...                   # any OpenAI-compatible provider

harnessadapter evolve prompt-only --evolver gepa \
  --environment my_benchmark/env.py:make_environment --environment-config env.yaml \
  --model accounts/fireworks/models/glm-5p3 --reasoning-effort medium \
  --provider-base-url https://api.fireworks.ai/inference/v1 --provider-env-key FIREWORKS_API_KEY \
  --set max_metric_calls=300

harnessadapter evaluate \
  --environment my_benchmark/env.py:make_environment --environment-config env.yaml \
  --split test --adapter w0=prompt-only --adapter gepa=.demo/evolution/evolve-gepa-XXXX/adapter \
  --output runs/test
```

`my_benchmark/env.py:make_environment` is any factory that returns an Environment (see below); the OfficeQA Pro one lives in `tasks/OfficeQA/host/environment.py`.
The provider defaults are `$OPENAI_BASE_URL` (else OpenAI) and `OPENAI_API_KEY`.
Settings are the keys of the Evolver's `evolver.toml` `[settings]`; override
them with `--set key=value` or `--settings file.yaml`, and unknown keys are
rejected. `--component FILE` chooses other adapter text files (default: the
context file). `--training-state DIR` resumes from a previous session's
`training_state/`. A launch YAML takes the same fields (`environment`,
`environment_config`, `settings`, `components`, `training_state`, `model`,
`provider_base_url`, `provider_env_key`, `output`) and runs with
`harnessadapter run`.

For a $0 end-to-end check, use the deterministic toy environment and a local
scripted model:

```sh
harnessadapter evolve prompt-only --evolver gepa --scripted-model \
  --environment harnessadapter.testing:KeywordEnvironment --set max_metric_calls=40
```

Set the reasoning effort explicitly for reasoning models. GLM-5.3 on Fireworks
at the provider default spent its whole 32K-token budget reasoning on GEPA's
reflection prompt and returned nothing; low, medium and high all finished.

## Session outputs

```text
.demo/evolution/evolve-<evolver>-XXXX/
  adapter/          the selected candidate, a git repository with one evolve commit; deploy it
  source/           snapshot of the starting adapter (never modified)
  training_state/   the optimizer's own state; pass it back to resume
  work/candidates/  every evaluated candidate, by content hash
  control/          result.json, evaluations.jsonl (every evaluate call), model-calls/ (every model call)
```

`candidate_ready` means the search finished and produced a loadable adapter.
Only a held-out evaluation says whether it is better.

## Writing an optimizer Evolver

A directory with two files. [`examples/greedy_reflection`](../../examples/greedy_reflection)
is a complete one in about 40 lines: each round it runs a train minibatch,
shows the failures to the model, asks for a rewrite, and keeps it only if the
val score improves.

```toml
# evolver.toml
kind = "optimizer"
plugin = "plugin.py:run"        # a file in this directory, or an importable module:function

[settings]                      # defaults; --set / --settings override them
rounds = 4
minibatch = 3
```

```python
# plugin.py
from harnessadapter.evolution.optimizer import Outcome

def run(context):
    best = context.seed                                  # {"ADAPTER.md": text}
    rollouts = context.evaluate(best, context.val)       # deploy, run, score
    ...                                                  # propose with context.model(prompt)
    return Outcome(best, {"seed_val_score": ..., "selected_val_score": ...})
```

```sh
harnessadapter evolve prompt-only --evolver ./my-optimizer --environment ... --model ...
```

What `run(context)` gets:

| | |
| --- | --- |
| `context.seed`, `context.components` | the adapter's optimizable texts `{path: text}` and their paths |
| `context.train`, `context.val` | the tasks it may evaluate; test is never available |
| `context.evaluate(candidate, tasks, label=...)` | deploys the texts as a complete adapter and returns one `Rollout` per task |
| `context.model` | the Evolver model, `str \| messages -> str`, every call recorded; `.serve()` gives a library with its own OpenAI client a local endpoint to the same model |
| `context.settings` | `[settings]` with this run's overrides |
| `context.state_dir` | the Training State: write your state here to support `--training-state` |
| `context.log` | progress messages |

Everything else is the framework's: the source snapshot, materializing and
caching candidates, re-running episodes the infrastructure lost, the call
ledger, split checks on resume, the committed candidate, and `result.json`.

## The Environment contract

`harnessadapter.environment`:

```python
class Environment:
    def tasks(self, split) -> list[Task]           # train | val | test; no answers
    def evaluate(self, adapter, tasks, *, label) -> list[Rollout]
    def describe(self) -> dict                      # recorded next to every result; no secrets

Task(id, split, prompt, kind)
Rollout(task_id, split, score, output, status, feedback, trace, steps, record, infra)
```

`evaluate` deploys the adapter directory to the acting harness, runs every
task, and scores it. A failed episode is a Rollout with score 0 and its status,
not an exception. The environment owns the answer key and decides what an
optimizer may learn from each split, from the task's registered split, never
the caller's label:

- **train**: the score, the agent's trajectory, and the grader's feedback,
  which may name the reference answer, as in both papers' feedback functions.
- **val**: the score and the trajectory, never the reference.
- **test**: never used by an optimizer. `context.evaluate` resolves every task
  by id against the registered train and val tasks (a test task is refused, a
  relabeled one runs as what it is) and returns rollouts without record
  locations.

This keeps a well-behaved plugin away from held-out data. It is not a sandbox:
a plugin is trusted code in the same process, so review third-party plugins as
you would any code.

A Rollout with `infra=True` was lost to the infrastructure (provider outage,
transport failure, a failing file or socket operation), not decided by the
agent. The framework re-runs such episodes twice more after a pause, for
optimizers and for `evaluate` alike. These re-runs are outside an optimizer's
own budget; `result.json` `rollouts` counts every episode run. What is still
lost:

- GEPA scores it 0 and does not reflect on it. It also skips a minibatch in
  which every remaining episode is already perfect.
- SkillOpt saves the finished episodes and stops with an error, rather than let
  a 0 bias its gate or read as a regression. A session resumed from its
  Training State runs only the missing episodes.
- `evaluate` leaves it out of the adapter's mean and reports `paired_mean` over
  the tasks every adapter scored.

An exception caused by the adapter itself (a renderer that raises) is a scored
failure, not an infrastructure loss.

SkillOpt runs with `eval_test` on, as upstream does, because that switch also
guards its final val pass that may promote the last (slow-updated) skill. Its
split has no test items, so its own test fields in `summary.json` are empty.
SkillOpt's own `openai_compatible` client reaches the session's model through
`context.model.serve()`, so both optimizers call an identically configured
model (reasoning effort included) and the key stays out of SkillOpt's
environment.

Resuming is refused when the environment's train, val or test assignment
differs from the one the Training State was built on.

## Current limits

- The text surface covers Markdown/text files. An optimizer that should also
  change tools, code, or roles needs a surface for them; learner Evolvers
  already can.
- SkillOpt's trainer keeps process-global backend state: run one SkillOpt
  session per process.
- Acting-harness cost comes from the environment's own records; `usage` in
  `result.json` is the Evolver model's.
