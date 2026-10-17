# Prompt-only on ServeLearnBench

The prompt-only baseline on a ServeLearnBench scenario: the same host, stream and scoring as
[MWM on ServeLearnBench](../mwm/README.md), with one learned `ADAPTER.md` as the host's context
and the Codex learner of the CL-Bench baseline (GLM 5.3 flash, one fresh session per block, tools gated to
`check_spec`).

```bash
.venv/bin/pip install "git+https://github.com/Infini-AI-Lab/ServeLearnBench"
FIREWORKS_API_KEY=... examples/servelearnbench/prompt_only/launch.sh retail_l3
```

The scores land in `~/code/metaworld-runs/slb_po_retail_l3/test.log`; the run and its settings are described in the
MWM example's README, which this launcher reuses with `MW_METHOD=prompt_only`.
