# Robosuite

The Robosuite tasks of [ASPIRE](https://research.nvidia.com/labs/gear/aspire/)'s Code-as-Policy harness (Agentic
Skills Discovery for Robotics, [arXiv:2607.00272](https://arxiv.org/abs/2607.00272),
[code](https://github.com/NVlabs/ASPIRE)) as a benchmark: for each task a session writes a program (Python against
the harness's perception, grasping and motion API), develops it on development seeds, checks it once on unseen seeds,
and is scored once on held-out seeds. The tasks run on the [cap-harness engine](../cap_harness/) (its simulator
service, `bin/sim`, its judge), which LIBERO shares; its README has the setup.

| Folder | What it is |
|---|---|
| `utils/benchmark/prepare.py` | The seven Robosuite tasks with their seeds, as task directories in Harbor's format (`tasks/`, each with its rerun, each read as its row through the engine's `utils/agent/fields.py`, the configs' `data.view`); `splits/` |
| `utils/benchmark/task-info/` | What a task and its record are, for the learner |
| `aspire/` | ASPIRE on Robosuite: `run.yaml` (ASPIRE's seeds), `smoke.yaml`, the `*_direct.yaml` comparison, `launch.sh`, and `tasks.sh`, which adds ASPIRE's verifiers to the tasks |

## ASPIRE on Robosuite

The curriculum run as ASPIRE runs it ([`aspire`](../../docs/methods/aspire.md)): ASPIRE's fix-loop subagent (the
actor), its skill verifier and its coordinator, each a Codex session given
ASPIRE's own prompt. For each task the actor debugs its program from the per-primitive traces and RGB keyframes; the
harness then scores it once on held-out seeds. After each task the coordinator updates the shared skill library,
admitting the actor's proposals only if a fresh verifier re-solves the task from the library and the proposals alone,
and chooses the next task. The actor's task functions are ASPIRE's (`hermit/agent/evolver/methods/aspire/actor.py`,
built on the engine's), and its verifier rows are ASPIRE's too (`aspire/tasks.sh` derives them from the tasks
`prepare.py` wrote).

On the host that serves the providers, with the engine set up:

```bash
export FIREWORKS_API_KEY=...
cd examples/robosuite/aspire
ASPIRE_ROOT=/path/to/ASPIRE ASPIRE_SIM_ENV=/path/to/sim_env.yaml ASPIRE_CONFIG=smoke.yaml ./launch.sh
ASPIRE_ROOT=/path/to/ASPIRE ASPIRE_SIM_ENV=/path/to/sim_env.yaml ASPIRE_CONFIG=smoke.yaml ./launch.sh eval --adapter final
```

`./launch.sh eval --adapter final` reruns every task with the final library (the test split's `rerun-*` rows):
ASPIRE's backward transfer, compared per task with the task's first pass during training (each training step's
score in the run's `steps.jsonl`; the rerun's in the eval's results). `--adapter root` is the empty library and
`--adapter none` no library at all: the baselines on the same seeds.

The protocol is ASPIRE's: each task learns on its development seeds and is scored once on held-out seeds; the
reruns score the same held-out seeds again, so the comparison reuses that partition (as ASPIRE's Phase 3 does).
The first evaluation ran `run.yaml` on held-out seeds 1-50 (half of ASPIRE's 1-100), and its reruns on the same
seeds. The held-out seeds are the task rows' (`prepare.py --eval`), so they are set when the launcher writes the
rows: `ASPIRE_EVAL_SEEDS=1-50 ./launch.sh`, and `ASPIRE_EVAL_SEEDS=1-50 ./launch.sh eval --adapter final` for the
reruns (an `agent.params.eval_seeds` override has no effect).

### Protocol

| | `run.yaml` | `smoke.yaml` |
|---|---|---|
| Tasks | the 7 Robosuite tasks, in the order the coordinator chooses (it starts from `splits/all.txt`'s first) | `cube_lifting`, `cube_stack` |
| Development seeds | 101-125 (ASPIRE's) | 101-103 |
| Unseen-check seeds | 126-130 (ASPIRE's), run once by the actor | 126 |
| Held-out seeds | 1-100 (ASPIRE's; `ASPIRE_EVAL_SEEDS` for others), run once by the judge | 1-3 |
| Fix loop | ASPIRE's: 3 versions per failure mode, 8 in total | the same |
| Evolutionary search | ASPIRE's, for a task the fix loop leaves below 90%: K = 8, top 3, stop at 80% or 5 rounds | the same |
| Verifier | ASPIRE's: 5 seeds (101-105), 2 attempts, verified at ≥ 4/5 | the same |
| Actor, verifier, coordinator | Codex, GLM 5.3 Flash (Fireworks), reasoning effort medium | the same |

