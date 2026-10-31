# OfficeQA

[OfficeQA](https://github.com/databricks/officeqa) ([dataset](https://huggingface.co/datasets/databricks/officeqa),
[paper](https://arxiv.org/abs/2603.08655)): questions about U.S. Treasury Bulletins (1939–2025) whose answers are
figures found in the bulletins, often after a computation. This example runs OfficeQA Full (246 questions) as the
[SkillOpt paper](https://arxiv.org/abs/2605.23904) does: its 50 / 24 / 172 train / validation / test split, each
question's parsed source pages in the prompt, and exact match. It trains the agent's skill, the adapter's
`ADAPTER.md`, with the GEPA and SkillOpt methods, with ReHarness a bank of reflections retrieved below that skill, and
with Meta-Harness Adapter whole adapters written by a coding-agent proposer (Codex harness only), and compares them
with no skill and with SkillOpt's initial skill, in both of the paper's harnesses: direct chat, with read and grep
tools limited to the question's source files, and Codex, a `codex exec` session with the whole corpus. The same
methods, Meta-Harness Adapter aside, are run a second time with Codex and GPT-5.6-Luna acting and learning on a
ChatGPT login, the second table of [Results](#results).

## Setup

Install the framework first ([Installation](../../docs/get-started/installation.md)) with the `skillopt` and `gepa`
dependency groups (pip 25.1+). Also needed: the gated Hugging Face dataset `databricks/officeqa` (`officeqa_full.csv`,
and `treasury_bulletins_parsed/` with `transformed/` and `jsons/`), and the key of the models' backend: for the default
(GLM 5.3 Flash acting, GLM 5.3 learning, on Fireworks) `FIREWORKS_API_KEY`. `model=<backend>/<model id>` on the
command line moves the agent, `llm=...` the learner ([Model backends](../../docs/guides/model-backends.md)). The Codex
harness also needs Docker, set up once as in the [recipe](../../docs/recipes/officeqa.md#run-in-the-codex-harness).

```bash
python -m pip install -e . --group skillopt --group gepa
python examples/officeqa/utils/prepare.py --csv /path/to/officeqa_full.csv --docs /path/to/treasury_bulletins_parsed
```

`prepare.py` writes the questions, the paper's split, SkillOpt's initial skill (`utils/seed_adapter/`) and a link to the
corpus (`utils/documents`, what the agent's sandbox is granted) to `utils/`.

## Run

```bash
hermit train --config examples/officeqa/gepa/run.yaml          # GEPA
hermit train --config examples/officeqa/skillopt/run.yaml      # SkillOpt
hermit eval  --config examples/officeqa/gepa/run.yaml     --adapter final                  # GEPA's skill
hermit eval  --config examples/officeqa/skillopt/run.yaml --adapter final                  # SkillOpt's skill
hermit eval  --config examples/officeqa/skillopt/run.yaml --adapter none                   # no skill
hermit eval  --config examples/officeqa/skillopt/run.yaml --adapter ../utils/seed_adapter  # initial skill
FIREWORKS_API_KEY=... WM_PORT=3601 examples/officeqa/reharness/launch.sh run        # ReHarness: the 50 as one stream
FIREWORKS_API_KEY=... WM_PORT=3601 examples/officeqa/reharness/launch.sh run eval   # its bank on the 172, three passes
FIREWORKS_API_KEY=... WM_PORT=3601 examples/officeqa/reharness/launch.sh stream     # the paper's protocol: the 172 as one stream
python examples/officeqa/meta_harness_adapter/make_seed.py              # Meta-Harness Adapter, Codex harness only
hermit train --config examples/officeqa/meta_harness_adapter/run.yaml   # six rounds of two candidates
hermit eval  --config examples/officeqa/meta_harness_adapter/run.yaml --adapter final
```

An adapter's accuracy is `mean_score` in `<method>/runs/<run name>/eval/<selector>/summary.json` (172 test
questions, three passes each), where `<selector>` is `final`, `none`, `adapter` or a directory's name such as
`seed_adapter`; `--adapter adapter` evaluates the trained adapter included in the folder. A smoke on a few
questions: the folder's `smoke.yaml`, after `hermit check --config ...`
([walkthrough](../../docs/recipes/officeqa.md#check-train-evaluate)). The Codex harness: the same commands with
`run_codex.yaml` and `smoke_codex.yaml`, the folder's configs with `agent.params.harness: codex` and no budget.

ReHarness runs through its launcher: it puts the embedding model behind a loopback relay for the adapter's reader
(whose sandbox has no keys), writes `reharness/seed_adapter/` and starts `hermit train`, `eval` or `check` on
`reharness/<config>.yaml`; `launch.sh smoke check --no-probe`, `launch.sh smoke` and `launch.sh smoke eval` are its
check and smoke. `run.yaml` is evaluated like the other methods (`reharness/runs/paper-reharness/eval/final/`);
`stream.yaml` has no test split, and its number is the mean of `rollout.mean_score` over the steps of
`reharness/runs/stream-reharness/steps.jsonl` (step 0, the bootstrap, has none). The
[recipe](../../docs/recipes/officeqa.md#reharness) explains the two protocols. Meta-Harness Adapter runs in the Codex
harness only (`OFFICEQA_CODEX_IMAGE` set as for the other Codex configs);
[`meta_harness_adapter/`](meta_harness_adapter/) describes its configs and records.

The second row of Results, Codex with GPT-5.6-Luna on the machine's ChatGPT login, runs the same configs through
[`luna/launch.sh`](luna/), which changes the models (`codexplan/gpt-5.6-luna` as the acting model; each learner call
answered by one Codex session through a relay, `utils/agent/codex_chat.py`, with `llm.timeout` 3,600 s in place of
the default 600 s; Harness²'s editors as Codex sessions), the Codex image and egress proxy (`OFFICEQA_CODEX_IMAGE`
`officeqa-codex:0.164.0`, `OFFICEQA_CODEX_PROXY` to chatgpt.com only), the run names and where the sessions'
workspaces go. [`luna/README.md`](luna/README.md) has the one-time setup (the image with the machine's standalone
Codex, the egress proxy) and the record of the runs:

```bash
RELAY_PORT=3672 examples/officeqa/luna/launch.sh relay      # the trainings' learner relay (the streams' on 3671)
examples/officeqa/luna/launch.sh none                       # no skill: skillopt/runs/luna-reference/eval/none
examples/officeqa/luna/launch.sh initial                    # initial skill: skillopt/runs/luna-reference/eval/seed_adapter
RELAY_PORT=3672 examples/officeqa/luna/launch.sh gepa 1     # ... gepa 4, skillopt 1 ... 4: one training, then its eval/final
examples/officeqa/luna/launch.sh ch                         # the Continual Harness stream; `h2`: the Harness² stream
MW_KEY_FILE=... WM_PORT=3691 examples/officeqa/luna/launch.sh rh   # the ReHarness stream (its embeddings stay on Fireworks)
```

## Results

Agent and learner: GLM 5.3 Flash (reasoning effort medium), with Codex CLI 0.162.0 and one container per session.
Each evaluation makes one pass over the 172 test questions.

| Method | accuracy |
|---|---|
| no skill | 0.541 |
| initial skill | 0.576 |
| **GEPA** | 0.580 |
| **SkillOpt** | 0.600 |
| **Meta-Harness Adapter** | 0.640 |
| **Continual Harness** | 0.634 |
| **ReHarness** | 0.634 |
| **Harness²** | 0.651 |

Results use two settings:

- **Train/test split:** [GEPA](../../docs/methods/gepa.md) and [SkillOpt](../../docs/methods/skillopt.md) train one
  `ADAPTER.md` from SkillOpt's initial skill on 50 training questions, select on 24 validation questions, and evaluate
  the fixed final adapter on 172 test questions. Accuracies are means of four independent trainings, with one
  evaluation pass per final adapter. Configs: [`gepa/run_codex_flash.yaml`](gepa/run_codex_flash.yaml) and
  [`skillopt/run_codex_flash.yaml`](skillopt/run_codex_flash.yaml). [Meta-Harness
  Adapter](../../docs/methods/meta_harness_adapter.md) searches on the 50 training questions alone (no validation
  questions) and evaluates its best candidate once on the 172; one training, in the Codex harness the streams use
  ([`meta_harness_adapter/run.yaml`](meta_harness_adapter/run.yaml), [result](meta_harness_adapter/#result)).
- **Test streaming:** [Continual Harness](../../docs/methods/continual_harness.md) and
  [Harness²](../../docs/methods/harness2.md) each process the 172 test questions once in the same order,
  updating the adapter as they go, without a separate training split or access to judge verdicts. These
  scores measure performance during adaptation. See [`continual_harness/`](continual_harness/) and
  [`harness2_stream/`](harness2_stream/) for their protocols.

**ReHarness** ([`reharness/`](reharness/), [method](../../docs/methods/reharness.md)) keeps the initial skill as
the harness's text and learns a bank of reflections below it, five retrieved per question by Qwen3-Embedding-8B and
revised after it, with the acting model as its reflection and revision agents. Each session is Codex in a container
of its own through [`utils/agent/codex_agent.py`](utils/agent/codex_agent.py) (3,600 s wall clock, as the two
streams; GEPA's and SkillOpt's trainings used SkillOpt's own Codex harness with 180 s sessions), which deploys the
skill and the question's retrieved reflections as the session's `AGENTS.md`. Five retrieved reflections run past
Codex's default limit on `AGENTS.md`, 32 KiB, beyond which Codex silently drops the rest, so the ReHarness configs
raise it to 200 KiB (`project_doc_max_bytes: 204800`); no session's `AGENTS.md` reached it.

- **Test streaming** ([`reharness/stream_codex.yaml`](reharness/stream_codex.yaml)): the 172 in the same seed-0
  order as Continual Harness and Harness², each answered by the bank as it stands, no verdict to the learner
  (`feedback: false`: the reflection and revision agents read the trajectory and the answer, never the judge's
  verdict or the reference answer). One stream on 2026-10-28: **0.634**, 0.640 over the first 86 questions and
  0.628 over the last 86; paired against Harness²'s reference execution of the initial skill (0.576), +5.8 points
  [−1.2, +13.4] (26 questions won, 16 lost). 10.0 h (USD 2.43 of sessions, USD 4.81 of reflection and revision
  calls), no errors, no session timed out; 172 reflections of 8.8 K characters on average.

Paired differences with 95% intervals, each run's selection and cost, the Codex sessions' isolation and the
differences from the paper: the [recipe](../../docs/recipes/officeqa.md#the-flash-row).

The earlier direct-chat runs (`run.yaml`, `stream.yaml`: SkillOpt's rollout, three passes) are in the recipe's
[direct-chat table](../../docs/recipes/officeqa.md#results); they are not on this row.

The same methods on a second row of settings: Codex CLI 0.164.0 with GPT-5.6-Luna (reasoning effort medium) acting
**and** learning on the machine's ChatGPT login (a Codex plan) instead of GLM 5.3 Flash on Fireworks. It is the
model of every session and, through a relay that answers each learner call with one Codex session, of GEPA's,
SkillOpt's, Continual Harness's and ReHarness's learners; Harness²'s editors are Codex sessions. Everything else is
the row above's: the data and split, the seed skill, the judge, the methods' parameters, one container per session,
the same stream order, one pass over the 172 test questions. Meta-Harness Adapter was not run on this row.

| Method | accuracy | GLM 5.3 Flash (above) |
|---|---|---|
| no skill | 0.308 | 0.541 |
| initial skill | 0.360 | 0.576 |
| **GEPA** | 0.600 | 0.580 |
| **SkillOpt** | 0.546 | 0.600 |
| **Continual Harness** | 0.477 | 0.634 |
| **ReHarness** | 0.517 | 0.634 |
| **Harness²** | 0.611 | 0.651 |

GEPA and SkillOpt are means of four trainings (0.593, 0.605, 0.622, 0.581 and 0.529, 0.523, 0.599, 0.535); the three
streams are one stream each; Harness²'s reference execution of the initial skill scores 0.390 (0.576 with GLM). What
the second row shows:

- **A lower start, larger gains, the same values.** Without a skill and with the initial skill GPT-5.6-Luna is 23
  and 22 points below GLM, and only GEPA ends level with it. The gains over the starting points are far larger than
  GLM's: GEPA − initial skill +24.0 [+16.7, +31.1] (GLM +0.4), Harness² − its reference execution +22.1 [+15.7,
  +29.1] (GLM +7.6). A check of every answer, one by one (2,408), finds the share with the right value, in whatever
  form, at 0.66–0.71 in every cell, 0.669 without a skill and 0.709 for Harness²; paired on the value alone, no
  method's interval excludes zero. The gains are answer form: GPT-5.6-Luna writes sentences, units and names inside
  `<answer>` by default (56 of the 172 answers with the initial skill carry the right value in a form the judge
  rejects), and the three methods that see the verdict or are told the form add the rule "only the value inside the
  tags" (GEPA's and SkillOpt's skills, 17 of the 21 harnesses in Harness²'s library: 94–98% of GEPA's answers are a
  bare value, against 37–44% in the three cells without a learned skill); Continual Harness's rule keeps "any
  required unit words" and ReHarness's skill is unchanged, its reflections mixed (52% and 65% of their answers are a
  bare value).
- **The methods' order changes.** GEPA beats SkillOpt, SkillOpt − GEPA −5.4 [−9.4, −1.5] paired over questions,
  where GLM did not separate them (+2.0 [−1.0, +5.1]); about two thirds of the gap is form, GEPA's "value only"
  being the more thorough. SkillOpt's four finals spread 0.523–0.599 (sd 0.035; GEPA's 0.017), so over trainings the
  difference is marginal (p ≈ 0.05).
- **Among the streams Harness² leads**, 0.611 against ReHarness 0.517 (−9.3 [−15.7, −2.9]) and Continual Harness
  0.477 (−13.4 [−20.3, −6.4]), the latter two not separated (+4.1 [−2.3, +10.5]), where GLM's three were within two
  points. A wrong form leaves a clean trajectory, so the two streams that never see the verdict learn it only in part
  (Continual Harness's learner saw a real command failure in 18 of its 172 steps and Codex's own hook notice in all
  of them; their answers with the right value in a form the judge rejects fall from the reference execution's 55 to
  36 and 26, where Harness²'s fall to 17), and Harness²'s editors are told the deliverable's form; the deliverable is
  also the question's fourth execution, after the editors read the first, where the other two streams score each
  question's first answer.
- **Conditions.** The two reference evaluations ran while the login was slow (4 and 3 sessions at SkillOpt's 180 s
  cap, scored 0: at most −2.3 and −1.7 points); the eight evaluations of trained skills had no timeouts. The runs
  took the plan's weekly window from 29% to 38%; no session was refused. Codex sessions record tokens, not prices
  (input, cached included, plus output): 3.4–4.0 M per evaluation; per training 4.1–4.5 M of training rollouts,
  20–23 M (GEPA) or 7–8 M (SkillOpt) of the sessions that score candidates (validation, minibatches) and 0.6–1.1 M
  of learner calls; per stream 46 M of sessions + 11 M of learner calls (Continual Harness), 29 M + 25 M (ReHarness)
  and, for Harness², 27 M of reference executions + 83 M of candidate executions (three per question, the
  deliverable among them) + 197 M of editor sessions.
- **Two defects of the ports, found on this row and not fixed here** (they touch the GLM row too):
  `utils/task-info/README.md` tells the learner the judge removes "unit words", where it removes million, billion,
  dollar and nominal and keeps `%`, thousand and trillion; only Continual Harness's Refiner reads it, and it wrote a
  rule keeping "required unit words" into its policy. The Continual Harness Evolver counts Codex's own notices (the
  hook-trust warning `codex_agent.py` always causes, reconnects) as failure signatures, in every step.

Paired differences with 95% intervals, the settings that differ from the row above, the check of the answers, the
trainings' selections and the login's rate limit: the
[recipe](../../docs/recipes/officeqa.md#the-flash-row-with-gpt-56-luna) and [`luna/`](luna/).

## Code

```text
utils/agent/        OfficeQAAgent (agent.py): SkillOpt's rollout, direct or Codex, with ADAPTER.md as the skill;
                    codex_sandbox.py and docker/ are the Codex harness's container, image and egress proxy
                    (Dockerfile.standalone: the image with a machine's standalone Codex); codex_login.py puts a
                    model on a ChatGPT login (per-session copies of the login, the plan's usage, session slots);
                    codex_chat.py is the learners' relay, one Codex session per chat-completions call
utils/prepare.py    tasks.jsonl, the splits, data.json and seed_adapter/      utils/judge.py   SkillOpt's exact match
utils/task-info/    the task for the learner
gepa/ skillopt/     smoke.yaml and run.yaml per method, smoke_codex.yaml and run_codex.yaml for the Codex harness;
                    adapter/ is the skill run.yaml trained (direct chat), with its .hermit/CARD.md
harness2_stream/    Harness² under the paper's protocol, by the upstream code, with Codex as the acting agent
                    (run_codex.yaml, slots_codex.yaml; utils/agent/codex_agent.py and codex_session.py; the
                    harness2-codex template); launch.sh starts a stream
reharness/          ReHarness: stream_codex.yaml (the 172 test questions as one stream, Codex as the acting agent),
                    run_codex_flash.yaml (the 50 as one stream, then the frozen bank on the 172), the direct-chat
                    smoke.yaml, run.yaml and stream.yaml; launch.sh starts the embedding relay and the command;
                    make_seed.py writes seed_adapter/, the initial skill over the reharness template
luna/               the second results row: launch.sh runs each method's Flash-row config with Codex + GPT-5.6-Luna
                    on the ChatGPT login (relay, none, initial, gepa N, skillopt N, ch, h2, rh, sweep);
                    slots_ctl.py sets the machine's session slots from the trainings' timeouts and 429s; README.md
                    is the setup and the record of the runs (harness2_stream/codex_proposer.py: Harness²'s editors
                    as Codex sessions)
```
