# OfficeQA

This recipe runs OfficeQA Full (246 questions about U.S. Treasury Bulletins) as the
[SkillOpt paper](https://arxiv.org/abs/2605.23904) does: the paper's 50 / 24 / 172 train / validation / test split,
each question's parsed source pages, SkillOpt's initial skill as the seed, and exact-match scoring. A GLM agent,
SkillOpt's own rollout, answers with the adapter's `ADAPTER.md` as its skill, in direct chat or in SkillOpt's Codex
harness, and the `gepa` or `skillopt` Evolver trains that skill from the scored answers. The code, configs and
trained adapters are in `examples/officeqa/`
([README](https://github.com/Infini-AI-Lab/hermit/tree/main/examples/officeqa)). For an API-only
walkthrough without external dataset assets, use [First real-model training](../get-started/first-model.md); for
local training without model access, [Quick Start](../get-started/quickstart.md).

## Requirements and configuration

Complete [Installation](../get-started/installation.md) with the `skillopt` and `gepa` dependency groups, activate the virtual
environment, and run the commands from the repository root (dependency groups need pip 25.1+):

```bash
python -m pip install -e . --group skillopt --group gepa
```

Also needed:

- authorized access to the gated Hugging Face dataset `databricks/officeqa`: `officeqa_full.csv` and
  `treasury_bulletins_parsed/` with its `transformed/` and `jsons/` directories;
- `raw.githubusercontent.com`, from which `prepare.py` fetches the split, or a local copy of it (`--split-dir`);
- the models' backend key: `FIREWORKS_API_KEY` for the configs as written;
- for the Codex harness only: Docker, npm to fetch Codex's Linux package, and an acting backend that serves the
  Responses API ([Run in the Codex harness](#run-in-the-codex-harness)).

The models are backend references ([Model backends](../guides/model-backends.md)). The acting agent is GLM 5.3 Flash
on Fireworks (`fireworks/accounts/fireworks/models/glm-5p3-flash`, the default of `utils/agent/agent.py`) and the
configs' learner (`llm`) GLM 5.3 on Fireworks, both at reasoning effort medium; the paper's learner is GPT-5.5 at
medium. `model=<backend>/<model id>` on the command line moves the acting agent; the configs set `llm` and keep their
learner unless `llm=...` is given too. Dollar accounting uses the backend's prices; verify them for your endpoint.

The agent acts in one of the paper's two harnesses (`agent.params.harness`), with the question and its parsed source
pages in either:

| | `direct` (default) | `codex` |
|---|---|---|
| Configs | `smoke.yaml`, `run.yaml` | `smoke_codex.yaml`, `run_codex.yaml` |
| Tools and files | glob, read and grep on the question's source files | a `codex exec` session with the whole corpus |
| Limits | 24 turns, replies of up to 16,384 tokens | 180 s per session, up to 24 sessions |

## Prepare the data

```bash
python examples/officeqa/utils/prepare.py --csv /absolute/path/to/officeqa_full.csv \
  --docs /absolute/path/to/treasury_bulletins_parsed
```

It writes into `examples/officeqa/utils/`:

```text
tasks.jsonl               the 246 questions, without answers
splits/                   train.txt, val.txt and test.txt (50 / 24 / 172 ids), and smoke_train.txt, smoke_val.txt
                          and smoke_test.txt: the first four training, two validation and four test questions
data.json                 absolute paths of the corpus, the answers and the CSV, and drifted_from_split
seed_adapter/             SkillOpt's initial OfficeQA skill as a prompt-only@1.0 adapter, where every config starts
.private/answers.jsonl    the answers, which the judge reads
documents                 a link to the corpus: what the agent's sandbox is granted (`.private/` is covered inside)
```

The split comes from SkillOpt's repository at a pinned commit unless `--split-dir` names a local copy of its
`data/officeqa_id_split`. Questions, answers and source fields come from the CSV, so they follow the dataset
revision you downloaded; `drifted_from_split` lists the items whose sources differ from the split's, which is the
paper's revision. `prepare.py` prints the split sizes and those ids. `data.json` holds absolute paths, so rerun
`prepare.py` after moving the dataset.

## Check, train, evaluate

Inspect `examples/officeqa/gepa/smoke.yaml`. It selects `prompt-only@1.0`, starts from `../utils/seed_adapter`,
uses the `gepa` Evolver with GLM 5.3 as its model, batches of two tasks, two validation tasks, one epoch, and a
training budget of USD 5 / three hours. Usage becomes visible at call boundaries, so the budget is not a
provider-side spending cap. The separate `check` probe and `eval` calls are outside the training budget.

```bash
hermit check --config examples/officeqa/gepa/smoke.yaml
hermit train --config examples/officeqa/gepa/smoke.yaml
hermit eval --config examples/officeqa/gepa/smoke.yaml --adapter final
hermit eval --config examples/officeqa/gepa/smoke.yaml --adapter ../utils/seed_adapter
hermit eval --config examples/officeqa/gepa/smoke.yaml --adapter none
```

`check` runs one training task without an adapter to probe the pipeline and judge; use `--no-probe` to skip that
model call. `train` executes two steps. On each step it runs the acting agent, scores the answers, lets GEPA reflect
on them and propose a new skill (kept if it does better on the same tasks, then scored on the validation tasks), and
checks and saves the adapter GEPA hands back. `eval` separately measures a selected version on the four test tasks,
three passes each (`eval.repeats`): `final`, the seed skill the run started from, and no skill. A smoke run checks
that the integration works; four test questions cannot establish a performance gain, and no smoke scores are
reported on this page.

The configs request the Evolver's sandbox (`sandbox.network: restricted`, allowing only the learner's model endpoint
and passing only its key variable). The in-process `gepa` method calls the run's model tool directly; enabling the
sandbox does not turn the whole Python process into an isolated environment.

Paths in a config are relative to its file, so the run is under `examples/officeqa/gepa/runs/smoke-gepa/`
([Outputs and scoring](#outputs-and-scoring) lists what it holds). Read the trained skill and its card, and deploy
it into a separate generated workspace:

```bash
cat examples/officeqa/gepa/runs/smoke-gepa/checkout/ADAPTER.md
cat examples/officeqa/gepa/runs/smoke-gepa/checkout/.hermit/CARD.md
hermit deploy --adapter examples/officeqa/gepa/runs/smoke-gepa/checkout \
  --workspace .demo/officeqa-codex --host codex
```

Inspect its `AGENTS.md` before starting the host. This checks host integration; effectiveness in another host needs
its own evaluation.

Rerunning `train` resumes an unfinished run at its last completed step. For another experiment after this one
finishes, choose another name, for example `trainer.run_name=smoke-02`, and use the same override on the
corresponding `eval` commands. The framework does not choose the best checkpoint: `final` is the Evolver's final
selection, which without one is the last adapter. Both methods here make one: GEPA its best validation candidate,
SkillOpt its best skill, handed back as is (`promote_final: false`).

## Full runs

Each method folder has four configs, each with its own run name and adapter tree, so runs can train side by side;
do not mix their run-name or tree settings. All use the `prompt-only@1.0` template, start from
`../utils/seed_adapter` and evaluate three passes per test question. The full runs run eight rollouts at a time, the
smoke runs four.

| Config | Harness | Train / val / test | `step_size` | Epochs | Budget | `call_timeout_min` | `run_name` |
|---|---|---|---|---|---|---|---|
| `gepa/smoke.yaml` | direct | 4 / 2 / 4 | 2 | 1 | USD 5, 3 h | 60 | `smoke-gepa` |
| `gepa/smoke_codex.yaml` | Codex | 4 / 2 / 4 | 2 | 1 | none | 60 | `codex-smoke-gepa` |
| `gepa/run.yaml` | direct | 50 / 24 / 172 | 3 | 4 | USD 40, 30 h | 60 | `paper-gepa` |
| `gepa/run_codex.yaml` | Codex | 50 / 24 / 172 | 3 | 4 | none | 180 | `codex-gepa` |
| `skillopt/smoke.yaml` | direct | 4 / 2 / 4 | 2 | 2 | USD 5, 3 h | 90 | `smoke-skillopt` |
| `skillopt/smoke_codex.yaml` | Codex | 4 / 2 / 4 | 2 | 2 | none | 90 | `codex-smoke-skillopt` |
| `skillopt/run.yaml` | direct | 50 / 24 / 172 | 40 | 4 | USD 40, 30 h | 180 | `paper-skillopt` |
| `skillopt/run_codex.yaml` | Codex | 50 / 24 / 172 | 40 | 4 | none | 360 | `codex-skillopt` |
| `gepa/run_codex_flash.yaml` | Codex 0.162 | 50 / 24 / 172 | 3 | 4 | none | 180 | `flash-codex-gepa` |
| `skillopt/run_codex_flash.yaml` | Codex 0.162 | 50 / 24 / 172 | 40 | 4 | none | 360 | `flash-codex-skillopt` |

`call_timeout_min` is the time, in minutes, the Trainer allows each Evolver call (setup, a step, finalize); Codex
sessions are slower, so `run_codex.yaml` allows more. `gepa/run.yaml` uses GEPA's default reflection minibatch of 3
(`data.step_size`), Pareto selection over the 24 validation questions and 4 epochs over the 50 training questions
(68 steps), since the paper does not publish its GEPA settings. `skillopt/run.yaml` uses the paper's SkillOpt
hyperparameters: 4 epochs of batches of 40 (2 steps each), minibatch 8, merge 8, learning rate 4 decaying to 2,
validation gate, gated slow update, and meta skill, with `prompt_env: officeqa` and `trajectory_view: paper`.
The two `run_codex_flash.yaml` configs are `run_codex.yaml` with GLM 5.3 Flash as the learner too, one evaluation
pass, and the Codex 0.162.0 image (`OFFICEQA_CODEX_IMAGE=officeqa-codex:0.162.0`): the settings of
[the Flash row](#the-flash-row), shared with the Harness² stream.
[Harness²](../methods/harness2.md) has no training run: it processes the test questions as one stream, with Codex as
the acting agent; see [`harness2_stream/`](https://github.com/Infini-AI-Lab/hermit/tree/main/examples/officeqa/harness2_stream) for its protocol and result.
`skillopt/smoke.yaml` runs 4 training questions in 2 epochs of 2 steps. Run it first, then the full configs:

```bash
hermit train --config examples/officeqa/skillopt/smoke.yaml
hermit train --config examples/officeqa/gepa/run.yaml
hermit train --config examples/officeqa/skillopt/run.yaml
hermit eval --config examples/officeqa/gepa/run.yaml --adapter final
hermit eval --config examples/officeqa/skillopt/run.yaml --adapter final
hermit eval --config examples/officeqa/skillopt/run.yaml --adapter none
hermit eval --config examples/officeqa/skillopt/run.yaml --adapter ../utils/seed_adapter
```

Each row of [Results](#results) is the `mean_score` of one evaluation, in the folder named after its selector:

| Row | Config | `--adapter` | `summary.json` under `examples/officeqa/` |
|---|---|---|---|
| no skill | `skillopt/run.yaml` | `none` | `skillopt/runs/paper-skillopt/eval/none/` |
| initial skill | `skillopt/run.yaml` | `../utils/seed_adapter` | `skillopt/runs/paper-skillopt/eval/seed_adapter/` |
| GEPA | `gepa/run.yaml` | `final` | `gepa/runs/paper-gepa/eval/final/` |
| SkillOpt | `skillopt/run.yaml` | `final` | `skillopt/runs/paper-skillopt/eval/final/` |
| ReHarness, direct chat | `reharness/run.yaml`, through `reharness/launch.sh run eval` ([ReHarness](#reharness)) | `final` | `reharness/runs/paper-reharness/eval/final/` |

The Codex column uses `run_codex.yaml` in the same commands (run names `codex-gepa` and `codex-skillopt`), after the
setup below.

## Run in the Codex harness

The SkillOpt paper also runs its OfficeQA agent in a Codex harness: each rollout is a `codex exec` session in a
workspace-write sandbox without network or web search, in a fresh workspace with the skill as
`.agents/skills/skillopt-target/SKILL.md`, the question with its parsed source pages as `task.md`, and the whole
corpus under `docs/`. A session has 180 s; one that ends without an `<answer>` is run again with its reply, up to
24 sessions. SkillOpt's analyst also reads the summary SkillOpt writes of each rollout's sessions
(`codex_trace_summary.txt`). Each method folder has `smoke_codex.yaml` and `run_codex.yaml`, its `smoke.yaml` and
`run.yaml` with `agent.params.harness: codex` and no budget (and, in `run_codex.yaml`, a longer
`call_timeout_min`). Codex reaches the acting model through its backend's Responses API, with the provider settings
in a `CODEX_HOME` of its own and none of your Codex configuration or instructions.

Each Codex session runs in a Docker container of its own (`examples/officeqa/utils/agent/codex_sandbox.py`):

- Files: only its workspace, a fresh Codex home and the corpus (read-only), with a private `/tmp`.
- Network: the containers sit on an internal Docker network with no route out; their only way out is an HTTPS proxy
  (`utils/agent/docker/egress_proxy.py`) that opens connections to the model provider's API, refuses the rest, and
  logs each decision.
- Codex's sandbox: Codex's own workspace-write sandbox runs inside the container, as SkillOpt asks. The session's
  commands write only the workspace, `/tmp` and parts of the fresh Codex home, have no network at all, and run in
  user, mount and network namespaces of their own. Docker allows a container those namespaces only with `SYS_ADMIN`
  and without its seccomp and AppArmor profiles, so the container runs that way.
- Environment: the session's shell gets only Codex's core environment variables (no API key), with Codex's shell
  snapshot off (it would bring back the whole environment).
- Resources: each container is limited to 6 GB of memory, 2,048 processes and a 4 GB `/tmp`.

Build the image (Ubuntu 24.04 with python3 and Codex 0.134.0 for Linux arm64; on an x64 host pack
`@openai/codex@0.134.0-linux-x64`, unpack its `openai-codex-0.134.0-linux-x64.tgz`, and build with
`--build-arg TRIPLE=x86_64-unknown-linux-musl`), create the network and start the proxy once:

```bash
cd examples/officeqa/utils/agent/docker
npm pack @openai/codex@0.134.0-linux-arm64 && mkdir -p codex
tar -xzf openai-codex-0.134.0-linux-arm64.tgz --strip-components 1 -C codex
docker build -t officeqa-codex:0.134.0 .
docker network create --internal oq-sandbox
docker run -d --name oq-egress --restart unless-stopped --read-only --network bridge officeqa-codex:0.134.0 \
  python3 -u /opt/egress_proxy.py 3128 api.fireworks.ai:443
docker network connect oq-sandbox oq-egress
cd -    # back to the repository root
```

`OFFICEQA_CODEX_IMAGE`, `OFFICEQA_CODEX_NETWORK` and `OFFICEQA_CODEX_PROXY` name others (defaults
`officeqa-codex:0.134.0`, `oq-sandbox` and `http://oq-egress:3128`). The proxy above lets through only Fireworks:
for another backend, start it with that backend's `host:port` (a plain-HTTP or local endpoint cannot be reached from
the network). `docker logs oq-egress` lists every connection the sessions' Codex made or was refused. The agent
checks the image, the network, the provider's key and the corpus before its first session and stops if one is
missing.

Eight rollouts at a time per run start eight containers; give Docker's VM enough CPUs and memory for every run you start
side by side (the Codex-harness runs behind [Results](#results) were four side by side on 12 CPUs and 24 GB). The
container is root with `SYS_ADMIN`, so Codex's sandbox is what keeps a session's commands in: on macOS, Docker's VM
(colima, Docker Desktop) shares your home folder by default, so share only the corpus and the Codex workspaces' folder,
and keep the answers out of both.

```bash
hermit train --config examples/officeqa/skillopt/smoke_codex.yaml
hermit train --config examples/officeqa/gepa/run_codex.yaml
hermit train --config examples/officeqa/skillopt/run_codex.yaml
hermit eval --config examples/officeqa/gepa/run_codex.yaml --adapter final
hermit eval --config examples/officeqa/skillopt/run_codex.yaml --adapter final
hermit eval --config examples/officeqa/skillopt/run_codex.yaml --adapter none
hermit eval --config examples/officeqa/skillopt/run_codex.yaml --adapter ../utils/seed_adapter
```

Codex's workspaces are under `~/.cache/hermit/officeqa-codex-actor/` (`agent.params.codex_dir`), outside the
checkout, so no repository instructions reach the session; each rollout's commands are in its `codex_raw.txt`, except
for a session that timed out, whose `codex_raw.txt` SkillOpt leaves empty.

### What the isolation stopped

GLM sessions look for data the question lacks (exchange rates, price indexes) outside their workspace and on the
internet. Of the 3,438 rollouts of the Codex-harness runs behind [Results](#results) (25,151 commands in their
transcripts; a session that times out leaves none), about 200 tried the network (BLS, World Bank, FRASER) and about
310 looked outside their workspace. None got anything: names did not resolve, the image has no curl or wget, and the
container holds nothing else. The proxy let through only connections to the model provider and refused Codex's own
to chatgpt.com, ab.chatgpt.com and api.github.com.

## ReHarness

[ReHarness](https://arxiv.org/abs/2610.12668) (Xiao et al., 2026) learns a bank of reflections, one per question
seen, instead of one skill: before each question the harness retrieves the five reflections whose source questions
are closest to it (Qwen3-Embedding-8B, cosine) and appends them to the agent's context below `ADAPTER.md`; after
it, the Evolver revises each retrieved reflection from its own source trajectory with the new one as feedback, and
writes the question's reflection. The method is strictly online: one pass over a stream, one question per step, no
validation set, no replay ([ReHarness](../methods/reharness.md) has its controls). `examples/officeqa/reharness/`
runs it in direct chat with the `reharness` Evolver on the `reharness` template, starting from SkillOpt's initial
skill as `ADAPTER.md` (`make_seed.py` writes `seed_adapter/`: the skill, then the template's framing of the retrieved
reflections, over an empty bank), with GLM 5.3 Flash at medium effort as the acting model and as the reflection and
revision agents, as the paper uses one backbone for every agent. `utils/agent/agent.py` hands the question to the
adapter's context tool (`Session.context(query=question, task=row)`) and records what was retrieved in the output's
`extra["context_tool"]`.

Two protocols, because the paper's is not this page's:

| Config | Stream (`data.train`) | Test | `run_name` | The number |
|---|---|---|---|---|
| `reharness/smoke.yaml` | the 4 smoke training questions | the 4 smoke test questions, one pass | `smoke-reharness` | none: a smoke |
| `reharness/run.yaml` | the 50 training questions | 172, three passes | `paper-reharness` | `mean_score` in `reharness/runs/paper-reharness/eval/final/summary.json` |
| `reharness/stream.yaml` | the 172 test questions | none | `stream-reharness` | the mean of `rollout.mean_score` over the steps of `reharness/runs/stream-reharness/steps.jsonl` (step 0, the bootstrap, has none) |

`run.yaml` is the setting of [Results](#results): the bank is written from the 50 training questions in the split's
order, then frozen and evaluated on the 172 test questions like the GEPA and SkillOpt skills, so its row is
comparable with theirs. `stream.yaml` is the paper's protocol: the 172 test questions are themselves the stream;
each is answered with the bank written from the questions before it and scored before the Evolver reads it, so the
judge's feedback, which names the reference answer, reaches the bank only after the question was answered, as the
environment's reward does. Its number is not comparable with the Results table (the method learns from the test
questions themselves, and each is answered once), only with other methods run under the same protocol. Neither
protocol uses a validation split: there is no candidate selection, and `final` is the bank after the stream. All
three configs set `data.step_size: 1`, `data.shuffle: false` and `trainer.epochs: 1`, a budget of USD 5 / 3 h
(smoke) or USD 40 / 30 h, and `call_timeout_min: 30`.

The adapter's reader runs in the rollouts' adapter sandbox, which has no keys and reaches only the loopback
endpoints named in the environment, so the configs are started through `launch.sh`: it starts a wire meter
(`python -m hermit.runtime.wire_meter`) on `WM_PORT` in front of Fireworks that signs the reader's requests with
`FIREWORKS_API_KEY` (or reuses one already there), exports `REHARNESS_EMBED_URL` to it, writes `seed_adapter/`, and
runs the command with the Evolver's embedding model bound to the same relay (`evolver.params.embedding`), so the
meter's ledger, `reharness/runs/wire_ledger-<port>.json`, holds the whole embedding bill under the bucket
`officeqa_reharness_<config>`. The key comes from the environment or the file `MW_KEY_FILE` names and is written
nowhere. Further arguments go to `hermit` as they are:

```bash
FIREWORKS_API_KEY=... WM_PORT=3601 examples/officeqa/reharness/launch.sh smoke check --no-probe
FIREWORKS_API_KEY=... WM_PORT=3601 examples/officeqa/reharness/launch.sh smoke
FIREWORKS_API_KEY=... WM_PORT=3601 examples/officeqa/reharness/launch.sh smoke eval
FIREWORKS_API_KEY=... WM_PORT=3601 examples/officeqa/reharness/launch.sh run
FIREWORKS_API_KEY=... WM_PORT=3601 examples/officeqa/reharness/launch.sh run eval            # --adapter final
FIREWORKS_API_KEY=... WM_PORT=3601 examples/officeqa/reharness/launch.sh stream
```

`eval` takes `--adapter final` unless another selector is given (`--adapter seed_adapter` is the initial skill over
an empty bank, which retrieves nothing). An interrupted stream is resumed in place by the same command
(`resume_mode: auto`). The run's records are those of [Outputs and scoring](#outputs-and-scoring), plus the bank:
`<run>/checkout/reflections/<id>.json`, one per question of the stream, each with its revisions, and
`<run>/workspace/state/reharness.json`, what each step retrieved, revised and wrote.

The Codex-harness configurations are `reharness/stream_codex.yaml` and `reharness/run_codex_flash.yaml`; the
stream's number is in [the Flash row](#the-flash-row).

## Meta-Harness Adapter

Meta-Harness Adapter is the outer loop of [Meta-Harness](https://arxiv.org/abs/2603.28052) (Lee et al., 2026) with the
search confined to the adapter: the acting agent stays fixed, where Meta-Harness also rewrites the agent's own loop. A
coding-agent proposer reads every earlier candidate's files, scores and full trajectories and writes new candidates,
each is evaluated on a fixed search set, and the best there is kept ([Meta-Harness
Adapter](../methods/meta_harness_adapter.md) has its controls). `examples/officeqa/meta_harness_adapter/` runs it on
the Flash row's settings: the 50 training questions are the search set (`data.step_size: 50`, so one step evaluates
one candidate), the proposer is Codex with GLM 5.3 Flash at medium effort, and the acting agent is
`utils/agent/codex_agent.py` with `deploy: adapter` (`ADAPTER.md` as the session's `AGENTS.md`, every other file of
the adapter in its workspace). It starts from SkillOpt's initial skill (`make_seed.py` writes `seed_adapter/`) on the
`meta-harness-adapter` template, whose files outside `.hermit/` are all the proposer's to write.

| Config | Rounds × candidates | Epochs | `run_name` | The number |
|---|---|---|---|---|
| `meta_harness_adapter/demo.yaml` | 1 × 2 | 3 | `mh-demo` | none: a demo, no test evaluation |
| `meta_harness_adapter/run.yaml` | 6 × 2 | 13 | `mh-flash` | `mean_score` in `meta_harness_adapter/runs/mh-flash/eval/final/summary.json` |

```bash
python examples/officeqa/meta_harness_adapter/make_seed.py
export OFFICEQA_CODEX_IMAGE=officeqa-codex:0.162.0
hermit train --config examples/officeqa/meta_harness_adapter/run.yaml
hermit eval  --config examples/officeqa/meta_harness_adapter/run.yaml --adapter final
```

The proposer runs in the run's sandbox, with read access to the corpus and no network but its model's endpoint; the
acting sessions are containers of their own, as for the other Codex-harness methods. Each proposal step's
`step_NNNN/output/` holds the round's prompt, the proposer's session and its report;
`<run>/workspace/state/evolution_summary.jsonl` lists every candidate with its hypothesis, base and mean on the
search set. The result is in [the Flash row](#the-flash-row).

## ACE

[ACE](https://arxiv.org/abs/2510.04618) (Zhang et al., ICLR 2026) grows a playbook of bullets with helpful and
harmful counters, one training question at a time: the acting agent names the bullets it used, a Reflector tags
them and diagnoses the attempt (a wrong answer is run again with the reflection, up to three rounds), and a Curator
adds bullets ([ACE](../methods/ace.md) has its controls and its differences from the authors' code).
`examples/officeqa/ace/` starts from SkillOpt's initial skill followed by an empty playbook (`make_seed.py` writes
`seed_adapter/`) on the `ace` template, with `data.step_size: 1`, two epochs over the 50 training questions and
validation at each epoch's end; `final` is the playbook with the best validation score.

| Config | Epochs | `run_name` | The number |
|---|---|---|---|
| `ace/smoke_codex_login.yaml` | 1 | `smoke-login-ace` | none: a smoke, 4 training questions |
| `ace/run_codex_login.yaml` | 2 | `login-codex-ace` | `mean_score` in `ace/runs/login-codex-ace/eval/final/summary.json` |

Both run the acting sessions and the Reflector and Curator on a ChatGPT Codex login, no API key (the example's
[README](https://github.com/Infini-AI-Lab/hermit/tree/main/examples/officeqa/ace)). The run below is one training
with its four evaluations; its numbers do not compare with [the Flash row](#the-flash-row) (another model, another
Codex, three passes).

- Models: `gpt-5.6-luna` acting and learning, reasoning effort medium, on a ChatGPT login.
- Harness: Codex 0.165.0 (`officeqa-codex:0.165.0`), a container per session, four side by side; each Reflector
  and Curator session in a container of its own (`learner_container`).
- Data: the 50 / 24 / 172 split of SkillOpt `79124b3`, `data.shuffle_seed: 42`; `officeqa_full.csv` with SHA-256
  `b5eb1ce0cb34…`, eight items drifted from the split ([Reproduction limits](#reproduction-limits)).
- Run: `run_codex_login.yaml` of `25f33d5` with `trainer.run_name=login-codex-ace-luna56`,
  `trainer.tree_dir=runs/trees/login-codex-ace-luna56` and `agent.params.codex_dir=~/.cache/hermit/officeqa-ace-luna56`.
  The training's acting sessions kept Codex's home on disk, the evaluations' in the container's memory, as in that
  commit. The training stopped once on a full disk and resumed after step 86 (`trainer.resume_mode: auto`).
- Spending: a subscription has no per-token price. Training 1.6 h: 2.47 M tokens of training sessions, 5.22 M of
  Reflector and Curator sessions, 2.61 M of retry and validation sessions. The evaluations, about 1 h each, ran one
  after another within four hours, beside another run on the same login.

| Adapter | accuracy, three passes | − the initial skill, points |
|---|---|---|
| none | 0.310 | −7.4 [−11.2, −3.7] |
| the initial skill: SkillOpt's, with ACE's empty playbook (`seed_adapter`) | 0.384 | |
| `final`, best validation (epoch 1, 91 bullets) | 0.521 | **+13.8** [+8.5, +19.0] |
| the last playbook (`workspace/state/last_adapter`, 109 bullets) | 0.529 | **+14.5** [+10.1, +19.2] |

Paired per question, 95% bootstrap intervals; bold where the interval lies above zero.

- The last playbook and the validation choice are level: +0.8 [−2.7, +4.5] points, 22 questions better and 22
  worse. Validation scored 0.583 after epoch 1 and 0.500 after epoch 2: 24 questions do not separate them.
- The agent names the bullets it used on one line of its final message: 46 of the 100 training samples named at
  least one, 54 wrote `none`; the 63 citations all name bullets the playbook had, no bullet more than three times.
- An earlier run on `gpt-6-luna` (`login-codex-ace-v2`, 29 October) scored 0.223 without an adapter, 0.314 with the
  initial skill, 0.440 `final` and 0.519 with the last playbook. Another model and another day: the service drifts,
  so only evaluations made together are compared.

## Outputs and scoring

A run's records are under `examples/officeqa/<method>/runs/<run name>/` (`<run>` below):

| Path | Contents |
|---|---|
| `<run>/checkout/` | the current adapter: `ADAPTER.md` and `.hermit/CARD.md` |
| `<run>/steps.jsonl` | scores, changes, costs and commit IDs per step |
| `<run>/learning_material/` | results and available logs shown to the learner |
| `<run>/workspace/state/` | the method's candidates and records, saved per step in `step_NNNN/state/` |
| `<run>/step_NNNN/output/report.md` | the method's report for that step |
| `<run>/summary.json` | final tag, checkpoints and stop reason |
| `<run>/eval/<selector>/summary.json` | `mean_score` over all attempts, `solved_once`, `errors`, `cost_usd` |
| `<run>/eval/<selector>/results.jsonl` | per attempt: the judge's score and feedback, `tokens`, `wall_s`, log path |
| `examples/officeqa/<method>/runs/trees/<run name>/` | Git history of the adapter versions |
| `examples/officeqa/utils/agent/.logs/` | direct-chat rollouts: SkillOpt's prediction files |
| `~/.cache/hermit/officeqa-codex-actor/rollouts/` | Codex-harness rollouts, with `codex_raw.txt` |
| `docker logs oq-egress` | every connection the Codex sessions made or were refused |

`<selector>` is `final`, `none`, `adapter`, or a directory's name such as `seed_adapter`; re-evaluating a selector
replaces its records.

- Each answer is scored by SkillOpt's evaluator (`utils/judge.py`): exact match after its normalisation (lower case;
  commas, punctuation and unit words removed), with no numeric tolerance. Token F1 is recorded as `f1` and is not
  the score. An attempt without an answer scores 0.
- Accuracy is `mean_score` over 172 test questions × 3 passes = 516 attempts per adapter. The framework computes no
  intervals; those in [Results](#results) are 95% bootstrap intervals over questions, paired per question.
- A Codex session longer than 180 s is a wrong answer, as in SkillOpt. SkillOpt's pinned code reports it as
  `write() argument must be str, not bytes` (its timeout handler writes Codex's output as bytes), which is also the
  failure the learners see.
- A rollout the model API could not serve is run again later; after three attempts it is recorded as an error
  rather than a wrong answer, counted in the summary's `errors`, and scores 0.
- The judge's feedback names the reference answer, and the learners see it for training questions. The validation
  questions are given to the learners too (each card's warnings say so), so validation selection is part of
  training. Test questions are used only by `eval`.

## Results

All numbers come from the full configs (`run.yaml`, `run_codex.yaml`); none from a smoke run. Agent: GLM 5.3 Flash
(reasoning effort medium) in SkillOpt's rollout, in direct chat or in the Codex harness (Codex CLI 0.134 for Linux,
one container per session). Learner: GLM 5.3 (medium) for GEPA and SkillOpt; ReHarness's reflection and revision
agents are the acting model, GLM 5.3 Flash (medium) ([ReHarness](#reharness)). Test: 172 questions, three passes each.

| Skill | Direct chat | Codex harness |
|---|---|---|
| none: the agent without an adapter | 0.415 | 0.543 |
| SkillOpt's initial skill (`utils/seed_adapter/`) | 0.465 | 0.564 |
| GEPA's trained skill | 0.564 ([card](https://github.com/Infini-AI-Lab/hermit/blob/main/examples/officeqa/gepa/adapter/.hermit/CARD.md)) | 0.595 |
| SkillOpt's trained skill | 0.576 ([card](https://github.com/Infini-AI-Lab/hermit/blob/main/examples/officeqa/skillopt/adapter/.hermit/CARD.md)) | 0.628 |
| ReHarness's bank (`reharness/run.yaml`): the initial skill plus the five reflections retrieved per question, written from the 50 training questions | 0.579 | not provided |

Paired differences in points of accuracy, with 95% bootstrap intervals over questions; bold where the interval lies
above zero. In direct chat, the trained skills' wins over the initial skill were reported without their intervals,
and initial skill − none was not reported.

| Difference | Direct chat | Codex harness |
|---|---|---|
| initial skill − none | not reported | +2.1 [−1.7, +6.0] |
| GEPA − none | **+14.9** [+9.5, +20.5] | +5.2 [−0.2, +10.5] |
| SkillOpt − none | **+16.1** [+10.3, +22.1] | **+8.5** [+3.1, +14.0] |
| GEPA − initial skill | +9.9 (interval not listed) | +3.1 [−1.6, +7.6] |
| SkillOpt − initial skill | +11.0 (interval not listed) | **+6.4** [+1.4, +11.4] |
| SkillOpt − GEPA | +1.2 [−3.1, +5.4] | +3.3 [−0.8, +7.4] |

The two methods are not separated in either harness. GEPA − none in the Codex harness is borderline: without the
items whose sources drifted ([Reproduction limits](#reproduction-limits)) it becomes +5.8 [+0.6, +11.2]; every other
conclusion holds without them.

ReHarness is not in the differences table: the baselines' per-question results are not in the repository, so no
paired interval. Its own 95% bootstrap interval over questions is [0.514, 0.645], which covers both trained skills'
means; its run is described in the [example](https://github.com/haizhongzheng/hermit/blob/main/examples/officeqa/README.md#results).

- The agent starts higher in the Codex harness: it runs shell commands and its own scripts, without direct chat's
  turn and reply limits. The initial skill does not help there.
- Of each adapter's 516 Codex-harness test attempts, sessions past 180 s made 12 wrong without a skill, 17 with the
  initial skill, 6 with GEPA's and 16 with SkillOpt's.
- For reference, the paper's Codex harness (GPT-5.5) goes from 38.3 without a skill to 51.1 with SkillOpt; it
  reports no GEPA in that harness.

What each run handed back as `final` (validation mean); the direct-chat rows are from the included cards:

| Run | `final` |
|---|---|
| GEPA, direct chat | candidate 1 of 28 (0.708; initial skill 0.500) |
| SkillOpt, direct chat | its best skill, from step 5 (0.750) |
| GEPA, Codex harness | candidate 13 of 18 (0.750; initial skill 0.625) |
| SkillOpt, Codex harness | its one accepted update, from step 1 (0.708); it rejected the rest |
| ReHarness, direct chat | the bank after the 50th question, 49 reflections (strictly online: no validation, no selection) |

Neither Codex-trained skill contains a test answer.

The included cards also record the direct-chat runs' training time and spending, at the backend's Fireworks prices:

| Run | Steps | Training time | Rollouts | Evolver | Test evaluation of `final` |
|---|---|---|---|---|---|
| GEPA (`gepa/run.yaml`) | 68 | 6.48 h | USD 1.3869 | USD 7.4196 | USD 3.4533 |
| SkillOpt (`skillopt/run.yaml`) | 8 | 2.40 h | USD 1.3500 | USD 4.5337 | USD 3.4350 |
| ReHarness (`reharness/run.yaml`) | 50 | 1.39 h | USD 0.3978 | USD 0.8963 | USD 3.9676 |

These are observations for planning. Not recorded here:

- the cost and time of the no-skill and initial-skill evaluations;
- any cost or time of the Codex-harness runs: their skills and cards are not included, and Codex reports only token
  totals, so the agent records no dollar cost for its rollouts (`cost_usd` is null; tokens and `wall_s` are in each
  `results.jsonl`);
- any smoke-run measurement: a smoke budget (USD 5 / three hours) is a cap, not a measurement.

An earlier Codex-harness run on macOS, with Codex's own sandbox limited to reads of the system, the workspace and
the corpus, gave 0.539, 0.531, 0.591 and 0.597 for the four skills (none, initial, GEPA, SkillOpt). There `/tmp`
stayed shared and the sessions' shells inherited the environment of the shell that started the runs; those numbers
are not the results above.

### The Flash row

A second row of settings, on which every method in the repository is measured alike: GLM 5.3 Flash (medium) acts
**and learns**, every session is Codex 0.162.0 in a container of its own (the image `officeqa-codex:0.162.0`), the
test questions are evaluated **once** (`eval.repeats: 1`, a standard error of about 3.7 points per accuracy), the
learner's spending is recorded (Codex sessions record only tokens). Not comparable with the table above (Codex
0.134, GLM 5.3 as the learner, three passes). GEPA and SkillOpt were each trained four times from the same config
(`gepa/run_codex_flash.yaml`, `skillopt/run_codex_flash.yaml`; run names `flash-codex-gepa`, `-r2`, `-r3`, `-r4`
and `flash-codex-skillopt`, `-r2`, `-r3`, `-r4`), each `final` evaluated once; Meta-Harness Adapter was trained once
(`meta_harness_adapter/run.yaml`, run `mh-flash`) and its `final` evaluated once; Harness² and Continual Harness are
one stream each over the same 172 questions in the same order
([`harness2_stream/`](https://github.com/Infini-AI-Lab/hermit/tree/main/examples/officeqa/harness2_stream#result),
[`continual_harness/`](https://github.com/Infini-AI-Lab/hermit/tree/main/examples/officeqa/continual_harness#result)), which answer each question once while learning and are therefore not
evaluations of a frozen adapter.

| Method | Protocol | accuracy | Runs |
|---|---|---|---|
| none: the agent without an adapter | one pass | 0.541 | |
| SkillOpt's initial skill | one pass | 0.576 | |
| GEPA, `final` | train on 50 / select on 24 / test on 172 | **0.580** (sd 0.031) | 0.541, 0.610, 0.599, 0.570 |
| SkillOpt, `final` | train on 50 / select on 24 / test on 172 | **0.600** (sd 0.003) | 0.599, 0.605, 0.599, 0.599 |
| Meta-Harness Adapter, `final` | search on 50 / test on 172 | **0.640** | one training |
| Continual Harness, the stream's answers | stream over the 172, no training split, no judge verdict | **0.634** | one stream (the same seed-0 order) |
| ReHarness, the stream's answers | stream over the 172, no training split, no judge verdict | **0.634** | one stream (the same seed-0 order) |
| Harness², deliverable | stream over the 172, no training split, no judge verdict | **0.651** | one stream (seed 0); its reference execution of the initial skill: 0.576 |

Paired per question, 95% bootstrap intervals; a method's four runs are averaged per question first:

| Difference | points |
|---|---|
| initial skill − none | +3.5 [−2.3, +9.9] |
| GEPA − none | +3.9 [−2.3, +10.5] |
| SkillOpt − none | +6.0 [−0.3, +12.4] |
| GEPA − initial skill | +0.4 [−5.4, +6.2] |
| SkillOpt − initial skill | +2.5 [−3.6, +8.6] |
| SkillOpt − GEPA | +2.0 [−1.0, +5.1] |
| Harness² deliverable − its reference execution | **+7.6** [+1.7, +13.4] |
| Continual Harness − Harness²'s reference execution | +5.8 [−1.2, +12.8] |
| Continual Harness − Harness² deliverable | −1.7 [−7.6, +4.1] |
| ReHarness stream − Harness²'s reference execution | +5.8 [−1.2, +13.4] |
| Meta-Harness Adapter − Harness²'s reference execution | **+6.4** [+1.2, +11.6] |
| Meta-Harness Adapter − Continual Harness | +0.6 [−5.8, +7.0] |
| Meta-Harness Adapter − ReHarness stream | +0.6 [−5.2, +6.4] |

- GEPA's four trainings spread over 7 points of test accuracy for the same validation score (its `final` was
  candidate 3 of 21, 1 of 11, 9 of 23 and 12 of 23, validation means 0.708, 0.708, 0.750, 0.792; the initial
  skill's validation mean itself ranged 0.458–0.625 across the runs' own evaluations of it); SkillOpt's four `final`
  skills (its best skill from steps 1, 6, 2, 1; validation 0.750, 0.792, 0.750, 0.750) are different texts of
  3.2–13.9 KB that differ on 27–39 questions each while landing on the same total. Report the means, never one run.
- The learned skills quote training questions' answers and particulars (bare-number examples, a month count, a table's
  row label; Meta-Harness Adapter's `final` quotes two as bare-number examples); no `final` skill quotes a test answer
  (one quoted training answer, 2.916, is also a test question's).
- Of each evaluation's 172 sessions, 1–4 reached the harness's 180 s cap and scored 0 (4 without a skill);
  about one session in ten tried the network or searched the host's file system before answering, and found
  neither (the container holds the workspace and the read-only corpus; the session's commands have no network).
- Learner spending and training time, at the backend's prices: GEPA USD 0.09, 0.07, 0.06, 0.08 in 3.2, 2.5, 2.9,
  3.1 h; SkillOpt USD 0.15, 0.17, 0.15, 0.14 in 1.4, 1.3, 1.2, 1.3 h; Harness² USD 25.86 of editor sessions and
  USD 1.62 of executions in 20.5 h, about 0.16 USD per question against GEPA's and SkillOpt's 0.01 (the executions'
  Codex tokens are not priced); Continual Harness USD 3.67 of Refiner calls and USD 2.81 of executions in 8.6 h, about
  0.04 USD per question; Meta-Harness Adapter USD 8.39 of search-set sessions (650: 13 candidates × 50 questions) and
  USD 0.17 of proposer sessions in 1.5 h, and USD 1.82 for its test evaluation.
- Meta-Harness Adapter's proposer wrote six rounds of two candidates; the starting skill scored 0.60 on the search
  set, the candidates 0.64–0.72; `final` was 12 points above the starting skill there (0.72) and 6.4 above it on the
  test questions, the best of 13 candidates on 50 questions being an optimistic estimate. Every candidate changed
  `ADAPTER.md` alone; no script or other file was written.
- ReHarness's stream (`stream_codex.yaml`, `feedback: false`, Codex's `AGENTS.md` limit raised to 200 KiB) scores
  0.640 on the first half and 0.628 on the second; 10.0 h, USD 2.43 of sessions and USD 4.81 of reflection and
  revision calls (about 0.04 USD per question), no errors. With no verdict shown, the reflections still quote numbers:
  102 of the 172 contain their question's reference answer, because they record the agent's own answers and about
  two in three of those were right.
- The two streams are within one stream's noise of each other (11 questions only Continual Harness gets right, 14
  only Harness²); Continual Harness scores 0.663 on the first half of the stream and 0.605 on the second, Harness²
  0.640 and 0.663.
- Of the misses in each evaluation, about nine are the right number in the wrong form, mostly a missing `%` where
  the reference answer carries one (13 of the 172 do, and the reference answers are not consistent about it);
  the judge keeps `%` and strips commas, `$` and `million`.

## Included adapters

`examples/officeqa/gepa/adapter/` and `examples/officeqa/skillopt/adapter/` are the skills `gepa/run.yaml` and
`skillopt/run.yaml` trained in direct chat. Evaluate them with a directory selector:

```bash
hermit eval --config examples/officeqa/gepa/run.yaml --adapter adapter
hermit eval --config examples/officeqa/skillopt/run.yaml --adapter adapter
```

Numbers they copied from training answers are replaced by made-up numbers of the same form, so their cards' scores
are those of the adapters as trained; re-evaluate the distributed adapters when that distinction matters.

No ReHarness bank is included: its reflections quote the training questions' reference answers from the judge's
feedback (37 of the 49 in the direct-chat `run.yaml` bank), which the repository does not redistribute. Each
ReHarness run's card is in its checkout (`reharness/runs/<run>/checkout/CARD.md`), with the bank beside it.

## Reproduction limits

This setup differs from the paper's:

- Models: GLM 5.3 Flash acts and GLM 5.3 learns, instead of the paper's GPT-5.x and Qwen models.
- Tools: SkillOpt's offline file tools, instead of the paper's internal search.
- GEPA: the paper does not publish its GEPA settings; `gepa/run.yaml` uses GEPA's default reflection minibatch of 3,
  Pareto selection over the validation questions and 4 epochs.
- Codex harness: a Linux container per session with only the workspace and the corpus, and only the model provider
  reachable; Codex's shell snapshot off; reasoning effort medium (SkillOpt's Codex default leaves Codex's own);
  8 rollouts at a time (SkillOpt: 4).
- Data: questions and answers from the latest dataset revision. `data.json` lists the items whose sources changed
  since the paper's revision: 6 test and 2 training questions for the results above. The results hold without them,
  except that in the Codex harness GEPA − no skill becomes +5.8 [+0.6, +11.2].
- ReHarness: OfficeQA is not a benchmark of the ReHarness paper (CL-Bench, MemoryBench, MemoryArena; Claude Opus
  4.8, GPT-5.5 and Gemini 3.7 Flash), so its rows here are this repository's settings, not a reproduction of a
  published number: GLM 5.3 Flash as every agent, SkillOpt's initial skill as the text above the reflections, the
  venue's acting pipeline as the decision agent, cosine ranking. The shared stream withholds the judge's verdict
  (`feedback: false`) that the paper's environment outcome provides; the split run selects nothing, as the method
  has no validation step. The method page lists
  [where the integration departs from the paper](../methods/reharness.md#where-this-integration-departs-from-the-paper).

Hosted models and the dataset can change independently of the code, so the same commit need not give the same
numbers.

## Preserve the protocol

Preserve `slow_update_gate`, `trajectory_view`, `prompt_env`, and final-selection settings (`promote_final`) when
comparing protocols; see [SkillOpt](../methods/skillopt.md) for those controls and
[Evaluation](../guides/evaluation.md) for reporting. Record the framework revision, the SkillOpt and GEPA commits the
dependency groups pin, the dataset revision and `data.json`'s drift list, the split, the seed skill, the model identifiers and
reasoning effort, the complete config, the harness and Codex version, and the concurrency. Evaluate no skill, the
seed skill and the trained skill in the same harness, and keep validation and test separate: do not select a
checkpoint on the test split. A smoke run is not an experiment. [Reproduce a result](../tutorials/reproduce-a-result.md)
has a checklist.

For your own task, implement the agent and judge described in [Task integration](../framework/task.md). For how
the components exchange data, continue to [Using and training adapters](../framework/adapter-lifecycle.md).
