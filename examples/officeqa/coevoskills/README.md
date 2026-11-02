# CoEvoSkills on the fixed OfficeQA actor

This example keeps the repository's built-in `codex` acting agent, OfficeQA task hooks and judge
unchanged. The pinned upstream CoEvoSkills loop learns only harness adapter
files. Acting sessions and evolution/evaluation fixtures use Docker network none
with a Unix-socket model-only channel. There is one method and template:
`coevoskills` / `coevoskills@1.0`.

## Setup

Install the `coevoskills` and `skillopt` dependency groups. Keep an unmodified
CoEvoSkills checkout at `4380d4bff673dd6e1d58e5babeb2aaa0fe527119`. Linux, Docker, bubblewrap and
a prepared Codex container image are required. Configure a
Responses-compatible [model backend](../../../docs/guides/model-backends.md).
Set `agent.model.ref` to a configured `<backend>/<model>` and its API key on
the host; only a scoped relay key enters the task containers. Acting and learning
use the same model and effort. Alternatively, `run_codex_luna.yaml` uses an
existing `codex login` (`agent.model.login: chatgpt`) through main's shared login
and refresh handling. Its relay permits only native model requests and token
refresh, with no general web proxy. Prepare the image with the shared
[Codex Docker instructions](../../../docs/recipes/officeqa.md#run-in-the-codex-harness),
then set `agent.codex.container.image`. The integration forces network none and
adds its model-only relay; it needs no egress-proxy container.

Prepare the existing OfficeQA data using `examples/officeqa/utils/prepare.py`
first. Then, from the repository root:

```bash
export COEVOSKILLS_ROOT=/path/to/CoEvoSkills
python examples/officeqa/coevoskills/prepare.py
python examples/officeqa/coevoskills/prepare_seed.py
python examples/officeqa/coevoskills/launch.py \
  --config examples/officeqa/coevoskills/smoke_codex.yaml \
  --output examples/officeqa/coevoskills/runs/smoke
```

The method preparation creates generation fixtures only for
the training IDs in `utils/splits/train.txt`. Validation and test questions use
the existing OfficeQA actor directly, without extra Harbor task packages;
private scoring data stays outside model contexts and Docker image layers.
Registry access belongs to preparation. All task execution is offline. A missing
dependency or incompatible environment is an execution failure, with no runtime
network exception or altered grading assertion.

## Full evaluation

```bash
python examples/officeqa/coevoskills/launch.py \
  --config examples/officeqa/coevoskills/run_codex.yaml \
  --output examples/officeqa/coevoskills/runs/full \
  --train-workers 4 --eval-concurrency 4
```

The existing 50/24/172 split is retained. Parallel training workers run original
independent loops from the same seed; their packages are merged in training-ID
order and then frozen. Aggregate worker time retains the configured training
budget. With one worker, Hermit trains sequentially. No learning or selection uses
the validation or test questions.

The three test arms use the same actor and IDs:

- `none`: no adapter.
- `common_seed`: the original OfficeQA seed context.
- `final`: the frozen learned adapter.

`comparison.json` records counts, errors, accuracy and paired percentage-point
differences. Training scores are not test accuracy. Source snapshots, raw exports
and deployment hashes stay under the ignored run directory. The shared sandbox
relay checks isolation at each container startup.
Source/image or frozen-adapter changes fail the run. Actor/judge content pins
keep the original implementation fixed, including when running from a source archive.

## Result

One training run with `run_codex_luna.yaml`: GPT-5.6-Luna at medium effort acting and learning through
Codexs. One pass per arm over the same 172 test questions; the 24 validation questions were not used for selection.
The [recipe](../../../docs/recipes/officeqa.md#coevoskills) records the run and its settings.

| Adapter | Correct / test questions | Accuracy | Final improvement over this arm |
|---|---|---|---|
| `none` | 40/172 | 23.26% | +40.70 points |
| `common_seed` | 62/172 | 36.05% | +27.91 points |
| `final` | 110/172 | 63.95% | — |
