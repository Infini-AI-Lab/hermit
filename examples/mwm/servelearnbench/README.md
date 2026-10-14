# MWM on ServeLearnBench

MWM learns a [ServeLearnBench](https://github.com/Infini-AI-Lab/ServeLearnBench) scenario from its serving stream
and is scored by the benchmark's own per-window tests. Host and learner are GLM 5.3 flash on Fireworks.

Install the benchmark, then run one scenario from the repo root:

```bash
.venv/bin/pip install "git+https://github.com/Infini-AI-Lab/ServeLearnBench"
FIREWORKS_API_KEY=... examples/mwm/servelearnbench/launch.sh retail_l3
```

The launcher exports the scenario, trains in the background (one to two days for retail_l3), then plays the
per-window tests and prints the benchmark's scores to `~/code/metaworld-runs/slb_retail_l3/test.log`.

Complete guidance: [`docs/recipes/servelearnbench.md`](../../../docs/recipes/servelearnbench.md).

---
**Requirements**

The framework in `.venv/` (Python 3.12+); the `codex` CLI and bubblewrap (`bwrap`) on the PATH (the learner is a
Codex session in its own namespace). Both sides reach Fireworks through the framework's wire meter
(`harnessadapter/runtime/wire_meter.py`), a local proxy the launcher starts: it records the tokens per run and
rewrites the learner's requests the way Fireworks needs.

---
**Settings**

`MW_TAG` (the run's name, default `slb_<scenario>`), `WM_PORT` (the host meter's port, 3451), `MW_RUNS` (where
runs go, `~/code/metaworld-runs`), `MW_MODEL_ID` / `MW_EVOLVER_MODEL` (the host and learner models),
`MW_TRAIN_SPLIT` (another train split, e.g. `splits/smoke_serving.txt` for a 12-case smoke).
`launch.sh retail_l3 test` replays the tests alone, at any time.
