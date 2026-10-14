# ServeLearnBench as a task

[ServeLearnBench](https://github.com/Infini-AI-Lab/ServeLearnBench) measures whether an agent can infer undisclosed,
changing rules from its own serving experience. A scenario is a chronological stream of tasks in environment windows;
a hidden policy governs each window and changes between windows; after each serving task the agent is told only a
score, and held-out test tasks at the end of every window measure what it learned. Nine scenarios: retail, banking
and pitch, each at three tiers.

This directory turns one scenario into a task of this framework, so that any Evolver and template can train on its
serving stream and be scored the benchmark's way. The benchmark's own code plays every episode: its agent loop, its
budgets, its stamped instructions, its tools and its verifier. An adapter enters at the two places the benchmark
leaves open to a learning method: its context is appended to the system prompt, and the tools it declares (MWM's
`wm`) are added to the tool list.

```text
prepare.py        exports one scenario: tasks.jsonl, splits/, windows.json, the reference documents
pipeline.py       run(task, adapter): one episode through the benchmark's agent loop
judge.py          the benchmark's reward; feedback is the one line its methods see ("Score: n/100.")
windows.py        the per-window tests on the adapter the run had at that point; the `slb score` result file
slb/              the adapter's host-side interface and the learner's evidence writer
task-info/        the task description for the learner, and deployment/ for the MWM Evolver (see below)
```

## Install

```bash
pip install -e .                                                  # this framework, Python 3.12+
pip install "git+https://github.com/Infini-AI-Lab/ServeLearnBench"   # the benchmark (openai, httpx, pydantic)
```

## Prepare a scenario

```bash
python examples/tasks/servelearnbench/prepare.py --scenario retail_l3 --block 12
```

writes, next to this file:

| File | Contents |
|---|---|
| `tasks.jsonl` | one row per serving and test task: `id`, `scenario`, `phase`, `timestamp` (the stream position the benchmark stamps on the instruction), `block` (serving: the MWM block, position // 12 + 1), `instruction` |
| `splits/serving.txt` | the serving stream in order (the train split) |
| `splits/test_w<w>.txt` | window w's held-out test tasks |
| `splits/smoke_serving.txt`, `splits/smoke_test.txt` | the first 12 serving tasks and 4 test tasks of window 0 |
| `windows.json` | per window: stream range and task ids (host side only) |
| `task-info/deployment/environment/docs/` | the store's policy and workflow documents, one file per section, exactly as the agent's `read_docs` returns them |

Nothing a learner reads says where a window begins or ends: the rows carry no window index and no evaluator field
(ground truth, category, family); the evidence records carry the stream position and the reward only.

## Run an episode

The pipeline reads its settings from the environment (a launcher sets them; see
[`examples/mwm/servelearnbench/`](../../mwm/servelearnbench/)):

| Variable | Meaning |
|---|---|
| `SLB_WORKDIR` | the run's task directory: `evidence/` (the learner's store), `rows/`, `episodes/`, `results/` |
| `SLB_MODEL`, `SLB_BASE_URL`, `SLB_API_KEY`, `SLB_REASONING_EFFORT` | the host model and endpoint (defaults: GLM 5.3 flash on Fireworks, effort high, the paper's setting) |

Per episode it writes the benchmark's result row (`rows/<id>.json`: phase, window, category, reward, verifier
detail, trace, usage; the host-side archive), the record a learner may read (`episodes/<id>/record.json`, the task's
log), and for a serving episode its evidence file (`evidence/block<NNN>/e<pos>.json`).

The adapter's `wm` tool is served outside the environment's 50-call budget and capped at 100 calls per episode, as
in every MWM venue; the 100-turn cap of the benchmark still applies.

## Per-window tests and the score

```bash
SLB_WORKDIR=... SLB_BASE_URL=... SLB_API_KEY=... \
python examples/tasks/servelearnbench/windows.py --config examples/mwm/servelearnbench/retail_l3.yaml --run <run name>
slb score <SLB_WORKDIR>/results/retail_l3_mwm.jsonl
```

Test tasks never update a method, so the benchmark's scores come from replaying each window's test set on the
adapter the run had after that window's serving tasks: the Trainer keeps one commit per step, `windows.py` picks for
window w the last step whose tasks all lie in windows 0..w, runs `harnessadapter eval` on it with `splits/test_w<w>.txt`,
and assembles every row into `results/<scenario>_<method>.jsonl`, which `slb score` reads like any result file of
the benchmark. A window's tests can run while the run is still training; a window not yet trained is skipped.

If `data.step_size` does not divide a window's serving count, the block that straddles the window boundary is
not in that window's adapter (the benchmark's own runner updates at every window's end), so a few serving tasks go
unlearned for that window's test. retail_l3's windows hold 74 to 84 serving tasks (`windows.json`); with blocks of
12, between 0 and 11 tasks per window are learned only after that window's test.

## task-info/deployment (MWM)

The MWM Evolver needs the venue's deployment texts: `contract/<slot>.md` (the contract's venue slots, information
only), `delegation` (`none`: the benchmark's agent loop cannot start a sub-agent), `host.json` (the host-side
settings: the wm call sentence, the libraries a `wm run` program may import, the transport note) and
`environment/README.md` (what the learner's /environment holds). The retail texts are the ones metaworld's retail
venue used. Another scenario of the same domain reuses them; a banking scenario needs its own `domain_what.md`.
