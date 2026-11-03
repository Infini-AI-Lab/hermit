# Records of the GPT-5.6-Luna run

`luna-mh/`: the run of [the README's "With GPT-5.6-Luna"](../README.md#with-gpt-56-luna), before the agent layers
(PR #54's OfficeQA Codex agent, `deploy: adapter`): the final adapter (`adapter/`, the files of the run's final
commit), one record per test question (`test-final.jsonl`: no reference answers and no agent answers), the run's
summary with the test evaluation's (`run.json`) and its resolved config (`config.yaml`). The files are laid out as
RRSI Adapter's [`results/`](../../rrsi_adapter/results/README.md), whose README describes each field.
