# CoEvoSkills Adapter on OfficeQA

The [CoEvoSkills Adapter](../../../docs/methods/coevoskills_adapter.md) runs the CoEvoSkills Generator/Verifier loop
(Zhang-Henry/CoEvoSkills @ `4380d4b`) once per training question and keeps the executable Skill packages it exports
in the adapter, where Codex finds them as its native skills. The acting agent is the framework's Codex agent with
OfficeQA's task functions and judge (`../utils/`), unchanged; it is also the loop's fresh-execution oracle, through
the Trainer's `run_agent` tool.

## Setup

1. Prepare the OfficeQA data (`../utils/prepare.py`) and the Codex harness: the image, the `oq-sandbox` network and
   the egress proxy of the [recipe](../../../docs/recipes/officeqa.md#run-in-the-codex-harness).
2. Install the dependency groups and keep the upstream checkout the Evolver reads its prompts and meta-skills from:

   ```bash
   python -m pip install -e . --group coevoskills --group skillopt      # pip 25.1 or newer
   git clone https://github.com/Zhang-Henry/CoEvoSkills && git -C CoEvoSkills checkout 4380d4bff673dd6e1d58e5babeb2aaa0fe527119
   export COEVOSKILLS_ROOT=$PWD/CoEvoSkills
   ```

3. Write the training questions as Harbor task packages and the starting adapter:

   ```bash
   python examples/officeqa/coevoskills_adapter/prepare.py     # data/: packages/<id>/ and the corpus image (Docker)
   python examples/officeqa/coevoskills_adapter/make_seed.py   # seed_adapter/: SkillOpt's initial skill as ADAPTER.md
   ```

   A package holds the question, its runtime notes and, under `tests/`, the hidden test with the reference answer.
   The test reaches only the oracle's fresh container after the acting agent answered; the Generator's container,
   the image and every model call are without it. Only the 50 training questions get packages. Two OfficeQA pieces
   reach the Evolver through the configs: `oracle_io.py:deliver` writes the acting agent's answer where the hidden
   test reads it, and `path_map` names where the package container's corpus and answer file are in the acting
   agent's workspace.

## Run

```bash
hermit train --config examples/officeqa/coevoskills_adapter/run_codex_luna.yaml
hermit eval  --config examples/officeqa/coevoskills_adapter/run_codex_luna.yaml --adapter final
hermit eval  --config examples/officeqa/coevoskills_adapter/run_codex_luna.yaml --adapter seed_adapter
hermit eval  --config examples/officeqa/coevoskills_adapter/run_codex_luna.yaml --adapter none
```

| Config | Acting and learning model | Harness |
|---|---|---|
| `run_codex.yaml` | GLM 5.3 Flash, medium effort (the Flash row) | Codex 0.162.0 in a container per session, egress proxy |
| `smoke_codex.yaml` | as above | the smoke splits, two questions, one oracle check each, short clocks, USD 5 |
| `run_codex_luna.yaml` | GPT-5.6-Luna, medium effort, the ChatGPT login (the Luna column) | Codex 0.164.0, egress proxy to chatgpt.com |
| `smoke_codex_luna.yaml` | as above | the smoke splits, two questions, one oracle check each, short clocks |

A step takes four training questions (`data.step_size`) and runs their loops at once (`evolver.params.workers`);
they start from the same adapter and do not see each other's packages. The 24 validation questions are not used.
Every completion of the upstream roles is a call of the run's `llm`; the oracle's acting runs count toward the budget
like any `run_agent` run.

## What a run leaves

Per step, `step_NNNN/output/questions.json` (each question: installed or not, upstream's final verdict, the loop's
error) and per question `loop.log` and `loop/` (upstream's trial and result). The final adapter's `skills/` holds the
packages, one directory each, named `<question>-evo-<name>`.

## Result

Not run with these configs yet. An earlier run of this integration used a different harness (Docker network none
with a model relay, image `0.162.0`, its own no-adapter baseline of 0.233 against the column's 0.308) and is not
reported here.
