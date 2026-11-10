# CoEvoSkills Adapter on OfficeQA

The [CoEvoSkills Adapter](../../../docs/methods/coevoskills_adapter.md) runs the CoEvoSkills Generator/Verifier loop
(Zhang-Henry/CoEvoSkills @ `4380d4b`) once per training question and keeps the executable Skill packages it exports
in the adapter, where Codex discovers them as native skills. The acting agent uses OfficeQA's existing prompt,
answer extraction and judge. Its collection hook keeps the OfficeQA log and verifies the deployed skills before
the workspace is released. The training oracle calls this same acting agent through the Trainer's `run_agent` tool.

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

   `data/revision.json` records the corpus hash, the image tag and Docker's image ID from this build.
   `prepare.py --no-build` leaves `image_id` null. When building that staged context for an experiment, use
   `docker build --iidfile data/image-id.txt -t <image-tag> data/image` from this example directory with the tag
   in `revision.json`. Save the emitted ID with the run records. Rebuilding the same corpus can pick up different
   packages or a new base image.

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
| `smoke_codex.yaml` | as above | four training questions in batches of two, one host intervention per question, short clocks, USD 5 |
| `run_codex_luna.yaml` | GPT-5.6-Luna, medium effort, the ChatGPT login (the Luna column) | Codex 0.164.0, egress proxy to chatgpt.com |
| `smoke_codex_luna.yaml` | as above | four training questions in batches of two, one host intervention per question, short clocks |

A step takes several training questions (`data.step_size`: sixteen in `run_codex_luna.yaml`, four in
`run_codex.yaml`) and runs their loops at once (`evolver.params.workers`). Each Generator starts with its own task
and background notes. Its fresh oracle applies the new candidate to the common starting adapter with prior packages
removed. The 24 validation questions are not used. Every completion of the upstream roles is a
call of the run's `llm`; the oracle's acting runs count toward the budget like any `run_agent` run.

## What a run leaves

Per step, `step_NNNN/output/questions.json` (each question: installed or not, upstream's final verdict, the loop's
error) and per question `loop.log` and `loop/` (upstream's trial and result). An acting log with a nonempty skill
bank includes `coevoskills-integrity.json`, the fingerprint checked against the oracle's candidate. The final adapter's `skills/` holds the
packages, one directory each, named `<question>-evo-<name>`.

## Result

The current configs have no published evaluation, final adapter or per-question results. The earlier
[OfficeQA run summary](https://github.com/illusoryw/hermit/blob/aa831fc39a39228fc8082ffeef1f2d1cf128bf89/examples/officeqa/coevoskills/README.md#result)
reports 110/172 using a different adapter layout, Docker network, learner transport, concurrency and Codex image. The frozen-test
protocol here measures transfer to other OfficeQA questions. The paper evaluates skills on the task that produced them.
