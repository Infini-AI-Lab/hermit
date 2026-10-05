# Differences from the Current Prototype

:::{note}
This page lists what the evolution framework design (agreed 2026-10-05) changes relative to the code in this
repository today.
:::

| Current prototype | Design |
|---|---|
| `architectures/` hold starting adapters; the manifest has no learnable/fixed specification | Templates declare `structure`; the Trainer enforces it with `check_spec` and repairs violations |
| Protected paths are declared by the Evolver definition (`evolver.toml` `protected`) | Fixed parts are declared by the template |
| Manifests and configs in JSON (`adapter.json`) or TOML (`evolver.toml`) | All in YAML: `adapter.yaml`, `evolver.yaml`, `run.yaml`; data and records stay JSON / JSONL |
| Commands `deploy`, `evolve` (one evolution session) and `run` (a config) | `check`, `train` and `eval`, each taking `--config run.yaml`; deployment stays as it is |
| The Evolver is a prompt and a host agent | The Evolver is a class with a uniform interface; `AgentEvolver` and `CommandEvolver` cover the prompt-and-agent and external-program cases |
| No outer loop: data, epochs, rollouts and scoring are outside the framework | The Trainer runs epochs and steps, rollouts through the user's pipeline, scoring with the user's judge, budgets, checkpoints and resume |
| `mwm` keeps its journal and docket inside the adapter | Training State lives in the run directory; trajectories may still be learned content if the template declares a data path |
| `mwm`'s EVOLVER.md also describes how to use the MWM method | `EVOLVER.md` describes structure only; the method belongs to the Evolver |
| Candidates record content fingerprints; the learner sees a framework-maintained history | Every adapter is a commit in the tree's git repository; checkpoints and finals are tags; each adapter carries `CARD.md` |
| Learning material is one folder per session | `learning_material/step_NNNN/` per step in one standard format, with `index.jsonl` and a `current` link |
