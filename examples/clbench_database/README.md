# CL-Bench database exploration

A CL-Bench venue: one question of an order's stream against a database whose schema may drift; the host answers with SQL through the task's QUERY channel. The score is accuracy over the stream. CL-Bench owns the loop; the agent is a port of metaworld's host
seat that plugs the adapter in through `harnessadapter.Session`. The benchmark runs from a clbench checkout
(`MWM_CLBENCH`, default `~/code/clbench`) in its own virtualenv; nothing is imported from a metaworld checkout.

## Layout

```text
utils/agent/agent.py            CLBenchDatabaseAgent: the framework's entry; starts the runner and reads its result
utils/agent/clbench_host.py     starts the runner in clbench's virtualenv (one retry), collects its logs
utils/agent/database_runner.py    plays one question of an order's stream through CL-Bench's own runner
utils/agent/host.py             AdapterHost: the model call per turn with the adapter's context and tools, the turn records
utils/prepare.py    writes tasks.jsonl and one ordered split per order beside itself
utils/judge.py      the venue's score and feedback line
utils/task-info/                the task as the learner reads it; task-info/mwm/ holds the venue texts MWM needs (docs/methods/mwm.md)
utils/tests/                    parity of the framework's mwm texts with native metaworld (both venues), and the host test
mwm/                            run.yaml and launch.sh (meters, relays, one run per order), sql_tool.py (the learner's SQL channel)
prompt_only/                    the prompt-only baseline: the same host, a Codex learner with GLM 5.3 Flash
```

`clbench_host.py` and `host.py` are the same files in both CL-Bench tasks.

## Prepare

```bash
python examples/clbench_database/utils/prepare.py --clbench ~/code/clbench
```

## Run

```bash
MW_TAG=<tag> WM_PORT=<port> MW_ORDERS="0" examples/clbench_database/mwm/launch.sh           # MWM
MW_TAG=<tag> WM_PORT=<port> MW_ORDERS="0" examples/clbench_database/prompt_only/launch.sh   # the baseline
```

The launcher starts the wire meters and the learner relay, prepares one task directory per order, and runs
`harnessadapter train` with the config beside it. `MW_TRAIN_SPLIT` names another split (a prefix of an order's
stream) for a smoke run.

```bash
PYTHONPATH=$PWD ~/code/clbench/.venv/bin/python -m unittest examples/clbench_database/utils/tests/test_host.py
python -m pytest examples/clbench_database/utils/tests/test_parity_golden.py
```

## Methods

| Folder | Evolver | Host |
|---|---|---|
| [`mwm/`](mwm/) | the MWM learner, one session per block, over the venue's evidence store | GLM 5.3 Flash over the mwm adapter |
| [`prompt_only/`](prompt_only/) | the built-in Codex Evolver with GLM 5.3 Flash, tools gated to `check_spec` | the same host over a prompt-only adapter |
