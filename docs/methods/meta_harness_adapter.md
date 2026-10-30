# Meta-Harness Adapter

Meta-Harness Adapter is the outer loop of Meta-Harness (Lee et al., 2026,
[arXiv:2603.28052](https://arxiv.org/abs/2603.28052); [code](https://github.com/stanford-iris-lab/meta-harness))
with the search confined to the adapter. A coding agent, the proposer, reads a filesystem holding every earlier
candidate's files, scores and full execution traces, and writes k new candidates; each is evaluated on a fixed
search set and logged to the same filesystem; the best on the search set is selected. Choose it when you have a
labelled training split to search on, an acting agent you will not change, and a budget for evaluating every
candidate on the whole search set.

**What it is not.** Meta-Harness searches over the whole harness program, the acting agent's loop included: how it
calls the model, runs commands and decides it is done. Here the acting agent and its host are fixed, and the
proposer changes only the adapter: `ADAPTER.md`, which the agent reads as its instructions, and any other files it
adds, which reach the agent as the agent's own deployment allows. Results of this method are results of adapter
search under a fixed agent, not of Meta-Harness itself.

## Setup

```yaml
template: meta-harness-adapter
start_from: seed_adapter         # the starting adapter, the first candidate
evolver:
  name: meta_harness_adapter
  params:
    model: fireworks/accounts/fireworks/models/glm-5p3-flash   # the proposer's model
    k: 2                         # candidates per proposal round
    max_rounds: 6
data:
  step_size: 50                  # the whole training split (the search set) per step
  shuffle: false
trainer:
  epochs: 13                     # 1 + max_rounds x k: the starting adapter, then each candidate once
```

This fragment belongs in a complete [run config](../framework/configuration.md). What the method needs from the run:

- A training split, the search set. Every step evaluates one candidate on all of it, so `data.step_size` must cover
  it. Validation data is not used.
- An acting agent that delivers the adapter. `ADAPTER.md` always reaches the agent
  ([What an adapter can rely on](../framework/adapter.md#what-an-adapter-can-rely-on)); what happens to the
  adapter's other files depends on the agent, and the task information must say so, because the proposer learns
  it from there.
- A Codex or Claude Code CLI for the proposer, as the built-in [`codex` and `claude` Evolvers](agent.md) run it.

## The template

`meta-harness-adapter` fixes only `.hermit/`: its manifest and the proposer's guide, `EVOLVER.md`. Every other file of
the adapter may be added, changed or removed, and the template declares no structure, no check of its own and no
exports. The guide says what an adapter is, that `ADAPTER.md` is the agent's instructions at the start of every task,
and that anything else reaches the agent only if `ADAPTER.md` tells it what is there; what the adapter contains and
how it is organised is left to the proposer.

## One framework step

One step evaluates one candidate (`data.step_size` covers the search set, so a step is an epoch):

```{figure} ../../assets/meta_harness_adapter_step.svg
:alt: One Meta-Harness Adapter step. The Trainer's rollouts on the search set evaluate the handed-back candidate; the Evolver records the scores, runs a proposal round when nothing is queued, validates and stores the candidates, and hands back the next one. The proposer reads the whole filesystem D; finalize picks the best mean on the search set.
:width: 100%

One step: the rollouts evaluate the candidate handed back last; a proposal round runs only when nothing is queued.
```

1. The step's rollouts are the evaluation of the candidate handed back last time (the starting adapter, `seed`, on
   the first step). The Evolver records their per-task scores against that candidate.
2. When no proposed candidate is waiting, a proposal round starts: a proposer session, with `check_spec` as its only
   tool, writes k candidates. Each must be a complete adapter in its own directory; a candidate with a bad name, a
   symbolic link or a failing `check_spec` is rejected and recorded with its problems. Its `.hermit/` is always
   replaced with the template's. Valid candidates are stored and queued.
3. The next queued candidate is handed back, so the next step's rollouts evaluate it. With nothing queued
   (`max_rounds` reached, or no valid candidate), the best candidate so far is handed back and evaluated again; its
   score becomes the mean over all its evaluations.

`finalize` hands back the candidate with the best mean score on the search set; on a tie, the one evaluated first.

| Paper (Algorithm 1) | Framework |
|---|---|
| The filesystem D | The Evolver's `state/`: `evolution_summary.jsonl` (one line per candidate: id, round, hypothesis, base, mean score, where its files and rollouts are), `candidates/<id>/adapter/`, and the learning material's `step_NNNN/` (for each task its input, the agent's output, the judge's score and feedback, the full trajectory) |
| The proposer | An `AgentEvolver` session (Codex or Claude Code) started afresh each round with the round's prompt, `PROPOSE.md`; it reads D and writes into `scratch/candidates/` |
| Interface validation | `check_spec` on each candidate, by the proposer and again by the Evolver |
| Evaluation on the search set | The Trainer's rollouts of the next step, on the training split |
| Selection | `finalize`: the best mean on the search set |

The proposer never runs evaluations and never sees validation or test results. The round's prompt carries the
authors' hard rules: write no facts about individual search tasks or their answers into a candidate, deliver all k
candidates, do not run the evaluation.

## Parameters and defaults

```{literalinclude} ../../hermit/algorithms/evolvers/meta_harness_adapter/evolver.yaml
:language: yaml
```

| Parameter | Values / default | Effect |
|---|---|---|
| `k` | Positive integer / 2 | Candidates per proposal round (the paper: 2-3; its TerminalBench-2 run: 1) |
| `max_rounds` | Integer or null / null | Proposal rounds; null starts a new round whenever every proposed candidate was evaluated. With `max_rounds` R, `trainer.epochs` = 1 + R x k evaluates each candidate once; more epochs re-evaluate the best |
| `backend`, `model`, `args` | `codex` or `claude`; `<backend>/<model id>`; CLI arguments | The proposer and its model, as for the built-in [agent Evolvers](agent.md) |
| `tools` | List / `[check_spec]` | The Trainer tools the proposer may call; `run_agent` is left out on purpose: the evaluation is the Trainer's |
| `prompt` | File name / `PROPOSE.md` | The round's prompt; its placeholders (`{round}`, `{k}`, the paths) are filled per round |

The rollout cost is (1 + R x k) x |search set| rollouts for R rounds, plus one proposer session per round.

## Where this integration departs from the paper

| Dimension | The paper | Here | Kind |
|---|---|---|---|
| What the proposer may change | The whole harness program, including the acting agent's loop | Every file of the adapter outside `.hermit/`; the acting agent, its loop and the host stay fixed | forced by the framework |
| Selection | A population with a Pareto frontier (for example accuracy against context length); the frontier is tested | The best mean on the search set; one candidate is tested | decision |
| Parent selection | None fixed: the proposer chooses what to build on | The same: each candidate names its `base`, or none | matched |
| Proposer | Claude Code with Claude Opus 4.6 at maximum effort | Codex or Claude Code with the run's choice of model | a setting |
| Interface validation | The candidate imports | `check_spec` against the template | forced by the framework |
| Isolation | — | The proposer runs in the run's sandbox; candidates with symbolic links are rejected, since they are copied outside it | the reproduction's |

## Records to read after a run

| Path | Contents |
|---|---|
| `step_NNNN/output/report.md` | The candidate the step evaluated and its mean, the round's queued and rejected candidates, the one handed back |
| `step_NNNN/output/proposer_prompt.md`, `proposer_report.md`, `agent-codex.jsonl` | On a proposal step: the round's prompt, the proposer's own account of its candidates (when it wrote one) and its session |
| `workspace/state/meta_harness_adapter.json` | Every candidate: round, hypothesis, base, status, each evaluation's step and per-task scores; the queue |
| `workspace/state/evolution_summary.jsonl` | The proposer's view: one line per candidate |
| `workspace/state/candidates/<id>/adapter/` | Each candidate's files |

Evaluate the final adapter on the held-out split with the [same protocol](../guides/evaluation.md) as the methods it
is compared with.

## Example

`examples/officeqa/meta_harness_adapter/` runs it on OfficeQA's Flash row: the 50 training questions as the search
set, six rounds of two candidates, GLM 5.3 Flash acting and proposing through Codex; see its README and the [OfficeQA
recipe](../recipes/officeqa.md).
