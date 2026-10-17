# Prompt-only

Use `evolver.name: prompt_only` to rewrite learnable Markdown from task feedback. Each step normally makes one
learner generation request per configured file, with one additional request if no usable file is returned.
HTTP retries are separate provider attempts. Start here for an instruction-only adapter whose changes are easy
to inspect.

## Setup

```yaml
template: prompt-only@1.0
evolver:
  name: prompt_only
llm:
  base_url: https://your-endpoint.example/v1
  model: your-learner-model
  api_key_env: LEARNER_API_KEY
```

This fragment belongs in a complete [run config](../framework/configuration.md). Set the named environment variable
before running. The acting model is configured by your pipeline, separately from `llm`.

## How an update works

For the default single `ADAPTER.md`, expect one or two generation requests per step, excluding HTTP retries.
Multiple entries in `files` multiply that work.

The method reads scored examples, prioritizes failures, and asks the learner model to rewrite the configured files.
The Trainer then applies specification checks. There is no validation-based improvement gate in this method:
a structurally valid update can reduce task performance. The default final selection is the current adapter.

## Parameters and defaults

```{literalinclude} ../../harnessadapter/algorithms/evolvers/prompt_only/evolver.yaml
:language: yaml
```

Set overrides under `evolver.params`. `files` must refer to text paths that the template permits the learner to
edit. `max_examples` and `max_chars` bound evidence shown to the learner; `max_tokens` bounds the model response,
which can include reasoning tokens depending on the provider.

The [first real-model tutorial](../get-started/first-model.md) includes all data and code.
The [OfficeQA recipe](../recipes/officeqa.md) supplies a larger benchmark integration. Use a small smoke run before
increasing epochs or evidence size, and evaluate with the [same held-out protocol](../guides/evaluation.md).
