# MWM

MWM (the meta world model of the MetaWorld project) keeps what a learner has found out about a task as a world model,
packed as the adapter: a
short briefing for the acting agent (`consumption/brief.md`), dated memory entries (`memory/`), worked examples
(`examples/`), a journal of the learning sessions (`meta/journal.md`) and a docket of open questions (`meta/cases/`).
The acting agent gets the briefing and a catalog of the memory in its system prompt, and reads the rest through one
read-only tool, `wm` (`list`, `memory`, `recall`, `search`, `read`, `trace`). Here the learner is GLM 5.3 Flash with
eight file tools: it reads the earlier runs of its task, screenshots included, and edits only the Markdown files. It
never sees a score.

`evolver.name: glm_mwm` is an in-process Evolver in the example's folder, not a built-in one. Training starts from
`template/` (W0): the MWM files with no learned content.

## One step

1. The Trainer runs the current adapter once on the task. The pipeline runs the untouched W0 without the world model
   (arm B) and a learned adapter with it (arm C); see the [task](../tasks/visual_showcase/README.md#conditions).
2. The Evolver collects the evidence: the task's runs so far in this training run, labelled `B`, `C1`, `C2`, ..., each
   with its page, screenshots, capture summary, model requests and tool calls. Scores and judge records are left out.
3. It rolls the journal and caps the docket as the MWM method does, then starts the learner with `prompt.md` and the
   read-only method files in `method/` (contract, agenda, skills, patterns).
4. The learner reads the evidence, edits the adapter's learnable Markdown, checks it with `validate` (the Trainer's
   `check_spec`) and ends with a report. The step's output keeps `report.md`, `transcript.md` and `changes.diff`.
5. The Trainer checks the adapter again, repairs it if needed, commits it and tags it `<run_name>/ckpt-step-N`.

After `rounds` sessions, a step only runs the adapter, to measure it: no learner call, no edits. If a session fails or
changes nothing, the next step runs the same adapter again.

## Knobs

| Setting | Meaning |
|---|---|
| `evolver.params.rounds` | Learning sessions; later steps only measure (5) |
| `evolver.params.base_url`, `model`, `api_key_env`, `price` | The learner's model: GLM 5.3 Flash on Fireworks, through the Evolver's own client |
| `evolver.params.max_seconds`, `max_requests`, `max_tools` | Limits of one session: 900 s, 64 requests, 120 tool calls |
| `evolver.params.max_completion`, `max_response` | Completion tokens per session (65,536) and per response (32,768) |
| `evolver.params.cases_file` | The task's `cases.json`, the source of each run's brief in the evidence |
| `evolver.params.speedgate` | The task's optional speed gate (`null`: off) |
| `data.step_size` | Runs per step (1) |
| `trainer.epochs` | Steps, one per epoch (the training split is one task): `rounds` + 1 |
| `trainer.tree_dir` | One tree per task, so each task's adapters start from the same W0 |
| `trainer.budget`, `call_timeout_min` | Spending and time limits |
| `eval.repeats` | Runs of each adapter in `eval` (3) |

All parameters and defaults: [`visual_showcase/glm_mwm/evolver.yaml`](visual_showcase/glm_mwm/evolver.yaml).

## Examples

| Folder | Task | Configs |
|---|---|---|
| [`visual_showcase/`](visual_showcase/) | [Visual showcase](../tasks/visual_showcase/); GLM 5.3 Flash writes an animated web page and learns from the browser's screenshots | `smoke.yaml`: case `waves`, 2 steps: one learning session, then one measurement<br>`run_waves.yaml`, `run_lava.yaml`, `run_silk.yaml`: one adapter per case, 5 learning sessions and a measurement step, 3 eval repeats<br>`glm_mwm/`: the Evolver<br>`template/`: W0, the starting adapter |

## Run

Needs Node 20 or newer and Google Chrome (see the [task's requirements](../tasks/visual_showcase/README.md#requirements)).

```bash
npm ci --prefix examples/tasks/visual_showcase/runtime
python examples/tasks/visual_showcase/prepare.py
export FIREWORKS_API_KEY=...
harnessadapter check --config examples/mwm/visual_showcase/smoke.yaml --no-probe
harnessadapter train --config examples/mwm/visual_showcase/smoke.yaml
harnessadapter train --config examples/mwm/visual_showcase/run_waves.yaml
harnessadapter eval  --config examples/mwm/visual_showcase/run_waves.yaml --adapter waves-r5/ckpt-step-5
harnessadapter eval  --config examples/mwm/visual_showcase/run_waves.yaml --adapter none
harnessadapter eval  --config examples/mwm/visual_showcase/run_waves.yaml --adapter none \
  pipeline.run=../../tasks/visual_showcase/pipeline.py:run_direct trainer.run_name=waves-direct
```

Without `--no-probe`, `check` also makes one real run. `waves-r5/ckpt-step-N` is the adapter after N sessions (W1 to
W5); `final` is W5 with the Trainer's `CARD.md`. `--adapter none` runs arm B, and the two overrides run arm A (one
request, no tools) in a run of its own. Group eval results by `output.arm` too: a checkpoint whose sessions changed
nothing is still W0 and runs as arm B. The three cases can train at the same time; their browser captures take turns.

`check` and `train` warn that the sandbox is off. The Evolver runs inside the Trainer's process, which the sandbox
does not confine (see [Configuration](../../docs/framework/configuration.md#sandbox-network-and-warnings)); the
Evolver itself keeps the learner's file tools inside the step's folders.
