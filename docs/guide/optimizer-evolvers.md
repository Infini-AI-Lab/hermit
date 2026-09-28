# Optimizer Evolvers: GEPA, SkillOpt, and your own

MetaHarnessAdapter separates three choices that published methods usually
bundle together:

| Choice | Examples | Where it lives |
| --- | --- | --- |
| Architecture: what the adapter can contain | `prompt-only`, `mwm`, `lemma-board` | `harnessadapter/architectures/` |
| Evolver: how an adapter changes | session learners (`basic`, `mwm`); optimizers (`gepa`, `skillopt`) | `harnessadapter/evolvers/` |
| Environment: how a candidate is scored | OfficeQA Pro with GLM in native Codex; your benchmark | `tasks/<venue>/`, or any `module:factory` |

Any Evolver can be paired with any architecture it supports and scored by any
Environment. The deployment path is the same for every candidate (context
block, optional MCP tools, optional native roles). The held-out evaluation is
also the same, so a comparison between Evolvers changes only the Evolver.

## Two kinds of Evolver

- **Session Evolvers** (`harnessadapter evolve`) run a learner session, Codex
  or Claude Code, that reads LearningMaterial and edits the candidate with
  sandboxed file tools. See [Evolution](evolution.md).
- **Optimizer Evolvers** (`harnessadapter optimize`) are search algorithms.
  They propose adapter texts, roll them out in an Environment, and keep what
  scores better. GEPA and SkillOpt are bundled. Each runs its upstream
  library unchanged; the framework supplies the adapter object that library
  asks a new system to provide.

| | GEPA ([gepa-ai/gepa](https://github.com/gepa-ai/gepa), ICLR 2026) | SkillOpt ([microsoft/SkillOpt](https://github.com/microsoft/SkillOpt), 2026) |
| --- | --- | --- |
| Upstream entry point | `gepa.optimize()` | `ReflACTTrainer(cfg, env_adapter).train()` |
| What the framework supplies | a `GEPAAdapter` (`evaluate`, `make_reflective_dataset`) | an `EnvAdapter` (`rollout`, split loader) |
| Candidate | `{relative path: text}` over the adapter's text surface | one document of the adapter (its context file) |
| Search | reflective mutation, per-instance Pareto front over `val`, minibatch acceptance | bounded add/delete/replace edits, textual learning rate, strict `val` gate, rejected-edit buffer, slow update, meta skill |
| Training State | GEPA's `run_dir` (`gepa_state.bin`) | SkillOpt's `out_root` (`runtime_state.json`, `history.json`) |

## The Environment contract

`harnessadapter.optimize.environment`:

```python
class Environment:
    def tasks(self, split) -> list[Task]           # train | val | test; no answers
    def evaluate(self, adapter, tasks, *, label) -> list[Rollout]
    def describe(self) -> dict                      # recorded next to every result; no secrets

Task(id, split, prompt, kind)
Rollout(task_id, split, score, output, status, feedback, trace, steps, usage, record)
```

`evaluate` deploys the adapter directory to the acting harness, runs every
task, and scores it. A failed episode is a Rollout with score 0 and its status,
not an exception. The environment owns the answer key and decides what an
optimizer may learn from each split:

- **train**: the score, the agent's trajectory, and the grader's feedback,
  which may name the reference answer. This matches the feedback functions of
  both papers.
- **val**: the score and the trajectory, never the reference.
- **test**: never used by an optimizer. The session wraps the environment:
  `tasks()` answers only for train and val, every task a plugin asks to
  evaluate is resolved by id against those registered tasks (a test task is
  refused, a relabeled one is evaluated as what it is), and rollouts come back
  without record locations. Environments decide what feedback may reveal from
  the registered split of the id, never from the caller's label. The OfficeQA
  environment also keeps held-out episodes in a separate directory tree.

The wrapper keeps a well-behaved plugin away from held-out data; it is not a
sandbox. An optimizer plugin is trusted code in the same process: it holds the
optimizer model's key (SkillOpt drives its own client) and could reach
anything the process can. Review third-party plugins as you would any code.

A Rollout with `infra=True` was lost to the infrastructure (provider outage,
transport failure, a failing file or socket operation), not decided by the
agent. The session re-runs such episodes twice more after a pause, for
optimizers and for `evaluate` alike. What is still lost: GEPA scores it 0 and
does not reflect on it (a minibatch with no evidence left is skipped); SkillOpt
stops with an error rather than let a 0 bias its gate or read as a regression,
and the session resumes from its Training State; `evaluate` leaves it out of
the adapter's mean and reports `paired_mean` over the tasks every adapter
scored. An exception caused by the adapter itself (a renderer that raises) is
a scored failure, not an infrastructure loss.

Resuming (`--training-state`) is refused when the environment's train, val or
test assignment differs from the one the state was built on.

`harnessadapter evaluate` scores several adapters on one split with the same
environment. It is the comparison every Evolver is judged by.

## Running

```sh
pip install -e '.[gepa]'                       # GEPA core has no dependencies
pip install -e /path/to/SkillOpt               # SkillOpt's recipe lives in its source tree
export FIREWORKS_API_KEY=...                   # or any OpenAI-compatible provider

harnessadapter optimize prompt-only --evolver gepa \
  --environment tasks/OfficeQA/host/optimize_env.py:make_environment \
  --environment-config env.yaml \
  --optimizer-model accounts/fireworks/models/glm-5p3 \
  --set max_metric_calls=300 --output runs/sessions

harnessadapter optimize prompt-only --evolver skillopt \
  --environment tasks/OfficeQA/host/optimize_env.py:make_environment \
  --environment-config env.yaml \
  --optimizer-model accounts/fireworks/models/glm-5p3 \
  --settings skillopt.yaml --output runs/sessions

harnessadapter evaluate --environment tasks/OfficeQA/host/optimize_env.py:make_environment \
  --environment-config env.yaml --split test \
  --adapter w0=prompt-only --adapter gepa=runs/sessions/optimize-gepa-XXXX/candidate \
  --output runs/test
```

For a $0 end-to-end check of either optimizer, use the deterministic
environment and a local scripted model server:

```sh
harnessadapter optimize prompt-only --evolver gepa --fake-optimizer \
  --environment harnessadapter.optimize.testing:KeywordEnvironment --set max_metric_calls=40
```

Settings are the keys in each Evolver's `evolver.toml`. Unknown keys are
rejected. `--training-state DIR` resumes from a previous session's
`training_state/`.

## Session outputs

```text
optimize-<evolver>-XXXX/
  source/           snapshot of the starting adapter (never modified)
  candidate/        the selected adapter, a git repository with one optimize commit; deploy it
  training_state/   the optimizer's own state; pass it back to resume
  work/candidates/  every materialized candidate, by content hash
  control/          result.json, evaluations.jsonl (every rollout call), optimizer-calls/ (GEPA)
  report.md
```

`candidate_ready` means the search finished and produced a loadable adapter.
Only a held-out evaluation says whether it is better.

## Writing an optimizer Evolver

```text
my-optimizer/
  evolver.toml     kind = "optimizer"; plugin = "module:Class" or "file.py:Class"; [settings]
  README.md
```

```python
class MyOptimizer:
    name = "my-optimizer"

    def run(self, context):               # harnessadapter.optimize.plugin.OptimizeContext
        seed = context.seed()             # {relative path: text}
        adapter = context.store.path(seed)             # a deployable adapter directory
        rollouts = context.environment.evaluate(adapter, context.environment.tasks("val"), label="seed")
        ...
        return Outcome(candidate=best_texts, summary={"seed_val_score": ..., "selected_val_score": ...})
```

The plugin writes only under `context.state_dir` and `context.work_dir`.
`context.lm` is the session's optimizer model, a `str -> str` callable with a
call ledger. Pass `--evolver /path/to/my-optimizer`.

## Current limits

- The text surface covers Markdown/text files. An optimizer that should also
  change tools, code, or roles needs a surface for them; session Evolvers
  already can.
- SkillOpt's trainer keeps process-global backend state: run one SkillOpt
  session per process.
- Optimizer-model cost for SkillOpt comes from its own token tracker; GEPA's
  from the session ledger. Acting-harness cost comes from the environment's
  records.
- SkillOpt's `openai_compatible` backend does not send `reasoning_effort`; the
  provider default applies to its optimizer calls.
  `--optimizer-reasoning-effort` applies to ChatLM calls (GEPA's reflection).
