# Records of the OfficeQA runs

What each run in [the README's Result](../README.md#result) left, one directory per run: the final adapter, one
record per test question, the rounds' decisions, the run's facts and its config. The run directories themselves
(trajectories, every candidate, upstream's records per round) are not in the repository.

| Directory | Code | What it is | Test (172) |
|---|---|---|---|
| `luna-rrsi-p1` to `-p4` | [`859bda9`](https://github.com/Infini-AI-Lab/hermit/commit/859bda9cf975c2b795cedaa1ad768b9bf9ddd8d7) | a training with `run_luna.yaml`, then its H_T evaluated | 0.634, 0.640, 0.576, 0.616 |
| `luna-rrsi-eng-p` | `859bda9` | a training with `run_luna_eng.yaml` (the engineering instance's selection weights) | 0.622 |
| `luna-rrsi-h0-p` | `859bda9` | H_0 (`seed_adapter`) evaluated in the same window | 0.355 |
| `luna-rrsi-r2` to `-r5` | the Evolver of [`c8afc5e`](https://github.com/Infini-AI-Lab/hermit/commit/c8afc5ed66c9dafbb99a9e2ce24772c85023df2c) | before the port: a training with `run_luna.yaml` | 0.605, 0.581, 0.599, 0.622 |
| `luna-rrsi-eng` | the same | before the port: a training with `run_luna_eng.yaml` | 0.616 |
| `luna-rrsi` | the same | before the port: H_0 evaluated in the first training's window | 0.378 |

The runs on the port recorded commit `859bda9` and a clean tree. The runs before it ran in a local checkout whose
commit (`3891bb9`) is not published; its Evolver directory had the SHA-256 recorded in `run.json` (`9aa5c603…`),
the same directory as `c8afc5e`'s, and its acting agent was PR #54's OfficeQA Codex agent (`deploy: adapter`).
Meta-Harness Adapter's run on the same column is in
[`../../meta_harness_adapter/results/luna-mh/`](../../meta_harness_adapter/results/luna-mh/).

| File | What it holds |
|---|---|
| `adapter/` | H_T: the files of the run's final commit (`final_commit` in `run.json`), which `hermit eval --adapter final` evaluated; `.hermit/` is the template's, with the Trainer's card |
| `test-final.jsonl`, `test-seed_adapter.jsonl` | one line per test question, in the split's order: `id`; `score`, the judge's exact match; `value`, the README's answer-by-answer check (`exact`, `right value, other form`, `wrong value`, `no answer`); `answered`; `tokens`, the session's (`null` where it left no count); `wall_s`; `status` and `error`, the evaluation's. The reference answers and the agent's answers are left out, so that the test split's answers are not in the repository |
| `rounds.jsonl` | upstream's edit history (`workspace/state/rrsi/history.jsonl`), one line per edit and one for H_0: round `t`, `variant`, `edit_id`, `component`, `targets_mode`, `outcome` (`BASELINE`; `ACCEPTED`; `LOST`, admissible but another candidate won the round; `REJECTED`, not admissible), the candidate's `S` and `C` on the evolve set and their changes, and the selection's `detail`. The proposer's hypotheses, predicted questions and diffs stay in the run directory |
| `run.json` | the run's summary (steps, stop reason, spending, the final commit), the code it ran (`code`: the framework commit, whether its tree had changes, the Evolver directory's SHA-256, upstream's commit), delta's calibration, H_T and S*, and the test evaluation's summary |
| `config.yaml` | the config as the Trainer resolved it, the command line's settings included, with this machine's paths replaced by `<checkout>`, `<venv>` and `~`. H_0's evaluation on the port keeps none: it ran as below |

The runs on the port, from `examples/officeqa/rrsi_adapter/` (`run_luna_eng.yaml` for `luna-rrsi-eng-p`):

```bash
O=(agent.concurrency=6 agent.codex.container.proxy=http://oq-egress-chatgpt:3128 'sandbox.allow=[chatgpt.com]')
R=(trainer.run_name=luna-rrsi-p1 trainer.tree_dir=runs/trees/luna-rrsi-p1)
hermit train --config run_luna.yaml "${O[@]}" "${R[@]}"
hermit eval  --config run_luna.yaml --adapter final "${O[@]}" "${R[@]}"
hermit eval  --config run_luna.yaml --adapter seed_adapter "${O[@]}" \
  trainer.run_name=luna-rrsi-h0-p trainer.tree_dir=runs/trees/luna-rrsi-h0-p
```
