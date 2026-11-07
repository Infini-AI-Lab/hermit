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
on Fireworks (`fireworks/accounts/fireworks/models/glm-5p3-flash`, the configs' top-level `model`) and the configs'
learner (`llm`) GLM 5.3 on Fireworks, both at reasoning effort medium; the paper's learner is GPT-5.5 at medium.
`model=<backend>/<model id>` on the command line moves the acting agent; the configs set `llm` and keep their learner
unless `llm=...` is given too. Dollar accounting uses the backend's prices; verify them for your endpoint.

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
documents                 a link to the corpus: what the agent's sandbox shows (`environment.read`); `.private/` is not
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

The configs run the acting agent in the task sandbox (`environment`: its code, `data.json` and the corpus, the
`skillopt` package; never `.private/` or the judge) and the learner in the evolver sandbox, each given its model's
key variable and provider host. The in-process `gepa` method calls the run's model tool directly;
enabling the sandbox does not turn the whole Python process into an isolated environment.

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

Each method folder has six configs, each with its own run name and adapter tree, so runs can train side by side;
do not mix their run-name or tree settings. All use the `prompt-only@1.0` template and start from
`../utils/seed_adapter`; all but the Flash-row and GPT-5.6-Luna configs (`run_codex_flash.yaml`,
`run_codex_luna.yaml`: one pass) evaluate three passes per test question. The full runs run eight rollouts at a
time, the smoke runs four.

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
| `gepa/run_codex_luna.yaml` | Codex 0.164, ChatGPT login | 50 / 24 / 172 | 3 | 4 | none | 180 | `luna-gepa` |
| `skillopt/run_codex_luna.yaml` | Codex 0.164, ChatGPT login | 50 / 24 / 172 | 40 | 4 | none | 360 | `luna-skillopt` |

`call_timeout_min` is the time, in minutes, the Trainer allows each Evolver call (setup, a step, finalize); Codex
sessions are slower, so `run_codex.yaml` allows more. `gepa/run.yaml` uses GEPA's default reflection minibatch of 3
(`data.step_size`), Pareto selection over the 24 validation questions and 4 epochs over the 50 training questions
(68 steps), since the paper does not publish its GEPA settings. `skillopt/run.yaml` uses the paper's SkillOpt
hyperparameters: 4 epochs of batches of 40 (2 steps each), minibatch 8, merge 8, learning rate 4 decaying to 2,
validation gate, gated slow update, and meta skill, with `prompt_env: officeqa` and `trajectory_view: paper`.
The two `run_codex_flash.yaml` configs are `run_codex.yaml` with GLM 5.3 Flash as the learner too, one evaluation
pass, and the Codex 0.162.0 image (`officeqa-codex:0.162.0`): the settings of
[the Flash row](#the-flash-row), shared with the Harness² stream. The two `run_codex_luna.yaml` configs are
`run_codex_flash.yaml` with the models changed (`gpt-5.6-luna` acting on a ChatGPT login, `login: chatgpt`, and
learning as one Codex session per chat call, `llm: {cli: codex, ...}` with `llm.timeout` 3,600 s) and the Codex
0.164.0 image: [the Flash row with GPT-5.6-Luna](#the-flash-row-with-gpt-56-luna); Continual Harness's and
ReHarness's `stream_codex_luna.yaml` likewise.
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

The SkillOpt paper also runs its OfficeQA agent in a Codex harness. Here every Codex run, of every method, is the
framework's Codex agent (`agent.name: codex`, [A coding-agent CLI as the acting agent](../framework/task.md#a-coding-agent-cli-as-the-acting-agent))
with OfficeQA's task functions (`utils/task.py`, `utils/prompt.md`): each rollout is one `codex exec` session in a
workspace-write sandbox without network or web search, in a fresh workspace with the adapter deployed as `hermit
deploy` deploys it (its context in `AGENTS.md`, its files under `hermit/<id>/`, named there; up to 64 KiB of
`AGENTS.md`, `project_doc_max_bytes`), the question with its parsed source pages as `task.md`, and the whole corpus
under `docs/`; one session of up to 3,600 s, whose last `<answer>...</answer>` is the answer. Each
method folder has `smoke_codex.yaml` and `run_codex.yaml`, its `smoke.yaml` and `run.yaml` with that agent and no
budget (and, in `run_codex.yaml`, a longer `call_timeout_min`). Codex reaches the acting model through its
backend's Responses API (or a ChatGPT login, `login: chatgpt`), with a home of its own and none of your Codex
configuration or instructions.

The Codex-harness numbers published below for no skill, the initial skill, GEPA and SkillOpt (the Codex column of
[Results](#results) and those rows of [the Flash row](#the-flash-row)) were made with SkillOpt's own Codex harness,
which this agent replaced: the skill as `.agents/skills/skillopt-target/SKILL.md` with a prompt line asking Codex to
use it, 180 s sessions, a session without an `<answer>` run again with its reply up to 24 times, and SkillOpt's own
trace summary for its analyst. Harness², Continual Harness, ReHarness's Codex stream and Meta-Harness Adapter were
made with this agent's deployment and session (formerly `utils/agent/codex_agent.py`, now removed). Compare rows made with one
harness; SkillOpt's Evolver now builds its Codex trace summary from the session's event stream.

Each Codex session runs in a Docker container of its own (`environment.image`, `hermit/agent/base/container.py`):

- Files: only its workspace, a fresh Codex home and the corpus (read-only), with a private `/tmp`.
- Network: the containers sit on an internal Docker network with no route out; their only way out is an HTTPS proxy
  (`docker/codex/egress_proxy.py`) that opens connections to the model provider's API, refuses the rest, and
  logs each decision.
- Codex's sandbox: Codex's own workspace-write sandbox runs inside the container. The session's
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
cd docker/codex
npm pack @openai/codex@0.134.0-linux-arm64 && mkdir -p codex
tar -xzf openai-codex-0.134.0-linux-arm64.tgz --strip-components 1 -C codex
docker build -t officeqa-codex:0.134.0 .
docker network create --internal oq-sandbox
docker run -d --name oq-egress --restart unless-stopped --read-only --network bridge officeqa-codex:0.134.0 \
  python3 -u /opt/egress_proxy.py 3128 api.fireworks.ai:443
docker network connect oq-sandbox oq-egress
cd -    # back to the repository root
```

Each config names its image in `environment.image` and its network and proxy in `environment.container`
(`run_codex.yaml` and `smoke_codex.yaml` `officeqa-codex:0.134.0`, the Flash row's configs `officeqa-codex:0.162.0`;
build that one the same way); override them on the command line (`environment.image=...`,
`environment.container.proxy=...`). The proxy above lets through only Fireworks: for
another backend, start one with that backend's `host:port` (a plain-HTTP or local endpoint cannot be reached from the
network); for a ChatGPT login, with `chatgpt.com:443 auth.openai.com:443`, and the model named by the login
(`agent.model.ref=null agent.model.name=gpt-6-luna agent.model.login=chatgpt`).
`docker logs oq-egress` lists every connection the sessions' Codex made or was refused. The agent checks the image
and the network before its first session, the task's `prepare` the corpus, and they stop if one is missing.

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

Codex's workspaces are under `~/.cache/hermit/officeqa-codex-actor/` (`agent.params.workspaces`), outside the
checkout, so no repository instructions reach the session; each rollout's events, commands included, are in its
`log/agent-codex.jsonl`, which the learning material copies.

### Through Harbor

The same Codex harness runs through [Harbor](../framework/task.md#benchmarks) instead of a container of the
acting agent's own (`environment.image`): each question is a Harbor task, and Harbor starts the container, installs Codex, deploys the
skill and runs the judge inside, after the session. Build the task set once (private: it holds the answers), then
run a method's `run_codex_harbor.yaml` with the corpus mounted where the tasks expect it:

```bash
python -m pip install --group skillopt                  # the task text is rendered by SkillOpt's own code
python examples/officeqa/utils/harbor/build_tasks.py    # utils/.private/harbor_tasks/<UID>/, tasks.jsonl, MOUNTS.json
export OFFICEQA_DOCS_ROOT=...                           # the mount the build prints: the corpus at /app/docs/root_1
hermit check --config examples/officeqa/gepa/run_codex_harbor.yaml
hermit train --config examples/officeqa/gepa/run_codex_harbor.yaml
hermit eval  --config examples/officeqa/gepa/run_codex_harbor.yaml --adapter final
```

What differs from the Codex route above: the container is Harbor's (`ubuntu:24.04` with python3 and ripgrep;
`build_tasks.py --base-image` takes one with Codex installed, which saves Harbor's install per trial), its network
is the task's (no egress proxy: the model's endpoint and key reach Codex through Harbor), the question and its
parsed pages are the instruction rather than a `task.md`, and the judge
(`utils/harbor/judge.py`, SkillOpt's exact match) reads the `<answer>` from the session's final message, or
`/app/answer.txt`, inside the container. The scores of this route are not in the results table: a run through it is
a different setting and is reported as one.

### What the isolation stopped

GLM sessions look for data the question lacks (exchange rates, price indexes) outside their workspace and on the
internet. Of the 3,438 rollouts of the Codex-harness runs behind [Results](#results) (25,151 commands in their
transcripts; a session that times out leaves none), about 200 tried the network (BLS, World Bank, FRASER) and about
310 looked outside their workspace. None got anything: names did not resolve, the image has no curl or wget, and the
container holds nothing else. The proxy let through only connections to the model provider and refused Codex's own
to chatgpt.com, ab.chatgpt.com and api.github.com.

### A model on a ChatGPT login

For a model the ChatGPT login serves ([the Flash row with GPT-5.6-Luna](#the-flash-row-with-gpt-56-luna)) the image
carries the machine's own standalone Codex and the egress proxy lets through chatgpt.com and auth.openai.com. `codex
login` on the machine once: the Codex agent (`login: chatgpt`) gives each session a copy of the login from one shared
copy (`~/.cache/hermit/codex-login/`), to which each session's refreshed token is written back, so a rotated token
reaches the next session. The learners' calls (`llm: {cli: codex, ...}`) are each one Codex session on the same
login in a bubblewrap sandbox of its own, started by the Trainer (its empty workspace, the login's hosts through a
proxy of its own; `isolation: container` gives it this image and proxy instead); Harness²'s editors are Codex sessions in the run's sandbox
(`evolver.params.backend: codex`). Sessions on the login share its plan, and files under
`~/.cache/hermit/codex-login/plan/` steer every running session: `PAUSE` holds new sessions back, a number in
`MAX_USED` holds them while the plan's fullest window is at or above that percent, a number in `SLOTS` caps how many
run at once on the machine; the plan's latest report is `plan.json` there. A session the plan refuses is run again
when its window resets, never scored; one that met "model is at capacity" is run again later.

```bash
cd docker/codex
cp -a ~/.codex/packages/standalone/releases/0.164.0-x86_64-unknown-linux-musl codex-standalone
docker build -f Dockerfile.standalone -t officeqa-codex:0.164.0 .
docker run -d --name oq-egress-login --restart unless-stopped --read-only --network bridge officeqa-codex:0.164.0 \
  python3 -u /opt/egress_proxy.py 3128 chatgpt.com:443 auth.openai.com:443
docker network connect oq-sandbox oq-egress-login
cd -
```

### Claude Code in the same containers

Claude Code runs in this setup too ([the Flash row with Claude Haiku 5.5](#the-flash-row-with-claude-haiku-55)): a
container per acting session on `oq-sandbox` (`environment.image`), an egress proxy of its own that lets through
`api.anthropic.com` and nothing else, and Claude Code's own sandbox inside for the session's commands (bubblewrap, no
network, writes to the workspace and `/tmp` only), as Codex's workspace-write sandbox runs in its. The image carries
the machine's own Claude Code, one file of the version the image is named for; the login's token reaches Claude Code
on a file descriptor, and the plan stays outside ([the guide](../guides/claude-subscription.md#execution-boundaries)).
The learners' calls (`llm: {cli: claude, ...}`) are each one Claude Code session on the same login in a bubblewrap
sandbox of its own, started by the Trainer (`isolation: container` gives it this image and proxy instead); Harness²'s
editors are Claude Code sessions in the evolver sandbox (`evolver.params.backend: claude`):

```bash
cp ~/.local/share/claude/versions/2.1.318 docker/claude/claude      # the machine's Claude Code, the native build
docker build --build-context codex=docker/codex -t officeqa-claude:2.1.318 docker/claude
docker run -d --name oq-egress-claude --restart unless-stopped --read-only --network bridge officeqa-claude:2.1.318 \
  python3 -u /opt/egress_proxy.py 3128 api.anthropic.com:443
docker network connect oq-sandbox oq-egress-claude
```

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
the framework's Codex agent (`ADAPTER.md` in the session's `AGENTS.md`, every other file of the adapter in its
workspace under `hermit/meta-harness-adapter/` and named in `AGENTS.md`). It starts from SkillOpt's initial skill (`make_seed.py` writes `seed_adapter/`) on the
`meta-harness-adapter` template, whose files outside `.hermit/` are all the proposer's to write.

| Config | Rounds × candidates | Epochs | `run_name` | The number |
|---|---|---|---|---|
| `meta_harness_adapter/demo.yaml` | 1 × 2 | 3 | `mh-demo` | none: a demo, no test evaluation |
| `meta_harness_adapter/run.yaml` | 6 × 2 | 13 | `mh-flash` | `mean_score` in `meta_harness_adapter/runs/mh-flash/eval/final/summary.json` |

```bash
python examples/officeqa/meta_harness_adapter/make_seed.py
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
| `ace/smoke_codex_luna.yaml` | 1 | `smoke-luna-ace` | none: a smoke, 4 training questions |
| `ace/run_codex_luna.yaml` | 2 | `luna-ace` | `mean_score` in `ace/runs/luna-ace/eval/final/summary.json` |

Both are in [the GPT-5.6-Luna setting](#the-flash-row-with-gpt-56-luna): the acting sessions on the machine's ChatGPT
login, the Reflector and Curator one Codex session per call (`llm: {cli: codex}`). The run below is one training with its four
evaluations, one pass each.

- Models: `gpt-5.6-luna` acting and learning, reasoning effort medium, on a ChatGPT login.
- Harness: the Codex agent, a container per session (`officeqa-codex:0.165.0`), four side by side.
- Data: the 50 / 24 / 172 split of SkillOpt `79124b3`, `data.shuffle_seed: 42`; `officeqa_full.csv` with SHA-256
  `b5eb1ce0cb34…`, eight items drifted from the split ([Reproduction limits](#reproduction-limits)).
- Run: `run_codex_luna.yaml` of `527b130`, run `luna-ace`.
- Spending: a subscription has no per-token price. Training 1.45 h: 18.5 M tokens of training sessions, 18.5 M of
  Reflector, Curator, retry and validation sessions. Each evaluation about 15 min.

| Adapter | accuracy | − the initial skill, points |
|---|---|---|
| none | 0.233 | −14.5 [−20.9, −8.1] |
| the initial skill: SkillOpt's, with ACE's empty playbook (`seed_adapter`) | 0.378 | |
| `final`, best validation (epoch 1, 79 bullets; [card](https://github.com/Infini-AI-Lab/hermit/blob/main/examples/officeqa/ace/adapter/.hermit/CARD.md)) | 0.640 | **+26.2** [+18.6, +33.7] |
| the last playbook (`workspace/state/last_adapter`, 106 bullets) | 0.593 | **+21.5** [+13.4, +29.7] |

Paired per question, 95% bootstrap intervals; bold where the interval lies above zero.

- `final` against the last playbook: +4.7 [−0.6, +10.5] points, 16 questions better and 8 worse. Validation scored
  0.708 after both epochs, so the first is kept.
- The agent names the bullets it used on one line of its final message: 81 of the 100 training samples named at
  least one, 142 citations in all.

## AHE Adapter

[AHE](https://arxiv.org/abs/2604.25850) (Lin et al., 2026) evolves the acting agent's harness over iterations of k
rollouts per task: an Agent Debugger reports on each task's traces, an Evolve Agent edits the harness and predicts what
each change fixes, and the next iteration's rollouts judge every change ([AHE Adapter](../methods/ahe_adapter.md) has
its controls and its differences from the authors' code). `examples/officeqa/ahe_adapter/` runs it on the `ahe`
template, with Codex as the acting agent, from the seed harness with SkillOpt's initial skill in its system prompt (`make_seed.py` writes
`seed_adapter/`): the 50 training questions as 100 trial rows (`make_rows.py`, k = 2), one step per iteration over all
of them, ten iterations; with no validation split, `final` is the best-ever iteration by training pass rate.

| Config | Steps | `run_name` | The number |
|---|---|---|---|
| `ahe_adapter/smoke_codex_luna.yaml` | 2 | `smoke-luna-ahe-adapter` | none: a smoke, 4 training questions × 2 |
| `ahe_adapter/run_codex_luna.yaml` | 10 | `luna-ahe-adapter` | `mean_score` in `ahe_adapter/runs/luna-ahe-adapter/eval/final/summary.json` |

Both are in [the GPT-5.6-Luna setting](#the-flash-row-with-gpt-56-luna): the acting sessions, the Agent Debugger and
the Evolve Agent on the machine's ChatGPT login, the learners as sessions of the `codex` Base Agent in the run's
sandbox. The run below is one training with two evaluations, one pass each.

- Models: `gpt-5.6-luna` acting, debugging and evolving, reasoning effort medium, on a ChatGPT login.
- Harness: the Codex agent, a container per session (`officeqa-codex:0.164.0`), eight side by side, 3,600 s each; the
  adapter's middleware as Codex hooks.
- Data: the 50 / 24 / 172 split of SkillOpt `79124b3` (validation unused), the training questions in order;
  `officeqa_full.csv` with SHA-256 `b5eb1ce0cb34…`, eight items drifted from the split
  ([Reproduction limits](#reproduction-limits)).
- Run: `run_codex_luna.yaml` of `725b457`, run `luna-ahe-adapter`.
- Spending: a subscription has no per-token price. Training 1.5 h: 190 M tokens of rollouts, 122 M of Debugger and
  Evolve sessions. Evaluations 9 min (`final`) and 21 min (H_0).

| Adapter | accuracy | − H_0, points |
|---|---|---|
| H_0: the seed harness with SkillOpt's initial skill (`seed_adapter`) | 0.366 | |
| `final`, the best-ever iteration 7 (training pass@1 0.69) | 0.616 | **+25.0** [+16.9, +33.1] |

Paired per question, 95% bootstrap intervals; bold where the interval lies above zero.

- Against the column: GEPA +1.6 [−3.2, +6.4], Harness² +0.6 [−5.2, +6.4], SkillOpt **+7.0** [+1.7, +12.4] (GEPA's
  and SkillOpt's four trainings averaged per question first). H_0 against the initial skill alone: +0.6 [−5.2, +6.4].
- Answers longer than 40 characters fall from 30% to 0.6%; answers containing the right value anywhere rise from
  0.558 to 0.616: most of the gain is the answer's form.
- Training pass@1 by iteration: 0.42, 0.61, 0.65, 0.66, 0.64, 0.64, 0.69, 0.67, 0.63, 0.65; no change was judged
  HARMFUL, so no automatic rollback ran.

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
| `examples/officeqa/<method>/.logs/rollout/<task>-<id>/agent-logs/0/` | direct-chat rollouts: SkillOpt's prediction files (each call's own `.logs`, the directory the agent sees beside its file; `agent.params.logs` moves them); the learner reads the rollouts' copies in `<run>/learning_material/step_NNNN/trajectories/`, and its own `run_agent` calls' in `<run>/agent_logs/` |
| `~/.cache/hermit/officeqa-*-codex*/<task>-<id>/` | Codex-harness runs: `work/` (the workspace), `log/` (the session's `agent-codex.jsonl`, prompt, last message, deployment, `task.md`), a model key's value scrubbed after the call |
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

### The Flash row with GPT-5.6-Luna

The seven methods of the Flash row once more, with Codex 0.164.0 and GPT-5.6-Luna (reasoning effort medium) acting
**and** learning on a machine's own ChatGPT login (a Codex plan) instead of GLM 5.3 Flash on Fireworks. The
[example's results](https://github.com/Infini-AI-Lab/hermit/tree/main/examples/officeqa#results) have the table and
what it shows; each method's `run_codex_luna.yaml` (`stream_codex_luna.yaml` for the Continual Harness and ReHarness
streams) is its Flash-row config with the models changed, run with the same commands
([Run](https://github.com/Infini-AI-Lab/hermit/tree/main/examples/officeqa#run); the image, the proxy and the
login: [A model on a ChatGPT login](#a-model-on-a-chatgpt-login)). The data, the split, the seed skill, the
judge, the
Evolvers' parameters, the session limits (SkillOpt's 180 s × 24 sessions for the trainings and the reference
evaluations, 3,600 s for the streams), the configured concurrency, ReHarness's embedding model (Qwen3-Embedding-8B on
Fireworks behind its launcher's wire meter: the plan serves no embeddings) and the one test pass are the Flash row's;
Meta-Harness Adapter was not run. RRSI Adapter ran later (`rrsi_adapter/run_luna.yaml` and `run_luna_eng.yaml`), on
the agent layers: the framework's Codex agent with its deployment (`ADAPTER.md` in `AGENTS.md`, every other file under
`hermit/rrsi-adapter/` and named there), the proposer and the search roles sessions of the `codex` Base Agent, behind
an egress proxy that lets through chatgpt.com only (`agent.codex.container.proxy` and `sandbox.allow` on the command
line; today's keys: `environment.container.proxy` and `sandbox.evolver.allowed_hosts`). What differs:

| Setting | The Flash row | With GPT-5.6-Luna |
|---|---|---|
| acting model | `fireworks/accounts/fireworks/models/glm-5p3-flash` through a Codex custom provider | `gpt-5.6-luna` with `login: chatgpt`: Codex's built-in provider on a per-session copy of the ChatGPT login (`hermit/agent/base/login.py`) |
| Codex's tools and prompt for the model | GLM is not in Codex's model catalog: default function tools (`exec_command`), the 20,751-character fallback prompt | Codex fetches gpt-5.6-luna's catalog entry from chatgpt.com at each session start: code mode only (a JavaScript `exec` tool that calls `exec_command`), a 17,730-character base prompt, verbosity low, tool output cut at 10,000 tokens; it stayed the same throughout the runs |
| learner (`llm`: GEPA, SkillOpt, Continual Harness, ReHarness's reflection and revision agents) | a chat completion to the same GLM (system prompt as such, `max_tokens` applied, 600 s timeout) | `llm: {cli: codex, model: gpt-5.6-luna, login: chatgpt}`: each call is one `codex exec` in a sandbox of its own (`hermit/agent/base/endpoint.py`; the runs reported here gave it a container, `isolation: container`), with Codex's base prompt and the call's system text as developer instructions; `max_tokens` is recorded, not applied (no reply came near one); `llm.timeout` 3,600 s |
| Harness² editors | Claude Code (`claude -p`) on GLM through a wire meter | Codex sessions in hermit's sandbox (`evolver.params.backend: codex`), `sandbox.allow: [chatgpt.com, auth.openai.com]` (today `sandbox.evolver.allowed_hosts`), a tree of its own |
| Codex | 0.162.0 (npm package), image `officeqa-codex:0.162.0` | the machine's standalone 0.164.0, pinned, image `officeqa-codex:0.164.0` (`docker/codex/Dockerfile.standalone`) |
| egress | `oq-egress`: api.fireworks.ai:443 only | `oq-egress-login`: chatgpt.com:443 and auth.openai.com:443 |
| concurrency on the machine | each run's own | each session on the login, acting or learning, holds one of the machine's session slots (`hermit/agent/base/login.py`: a number in the file `SLOTS`, read at every try) |
| run names, workspaces | `flash-codex-gepa`/`-skillopt{,-r2,-r3,-r4}`, `ch-stream-s0`, `rh-stream-s0`, `h2-codex-s0`; `~/.cache/hermit/...` | `luna-gepa`/`-skillopt{,-r2,-r3,-r4}` (the two reference evaluations under `luna-skillopt`), `luna-ch-s0`, `luna-rh-s0`, `luna-h2-s0`; `~/.cache/hermit/officeqa-codex-actor-luna` for the trainings and the reference evaluations, `officeqa-{ch,rh,h2}-codex-luna` for the streams |

The model change cannot be separated from the second, third and fourth rows: they come with running GPT-5.6-Luna in
Codex on the plan.

| Method | Protocol | accuracy [95% bootstrap interval over questions] | Runs |
|---|---|---|---|
| none: the agent without an adapter | one pass | 0.308 [0.238, 0.378] | 4 sessions at the 180 s cap, scored 0 |
| SkillOpt's initial skill | one pass | 0.360 [0.291, 0.430] | 3 at the cap |
| GEPA, `final` | train on 50 / select on 24 / test on 172 | **0.600** [0.533, 0.666] (sd 0.017) | 0.593, 0.605, 0.622, 0.581 |
| SkillOpt, `final` | train on 50 / select on 24 / test on 172 | **0.546** [0.481, 0.612] (sd 0.035) | 0.529, 0.523, 0.599, 0.535 |
| Continual Harness, the stream's answers | stream over the 172, no training split, no judge verdict | **0.477** [0.401, 0.552] | one stream (the seed-0 order) |
| ReHarness, the stream's answers | stream over the 172, no training split, no judge verdict | **0.517** [0.442, 0.593] | one stream (the seed-0 order) |
| Harness², deliverable | stream over the 172, no training split, no judge verdict | **0.611** [0.535, 0.680] | one stream (seed 0); its reference execution of the initial skill: 0.390 [0.320, 0.465] |
| RRSI Adapter, H_T | evolve on the 50 (two trials each) / test on 172 | **0.616** [0.552, 0.680] (sd 0.029) | 0.634, 0.640, 0.576, 0.616 (`rrsi_adapter/run_luna.yaml` at `859bda9` with `agent.concurrency=6`, ten rounds of two); one training with the engineering instance's selection weights (`run_luna_eng.yaml`) 0.622 [0.552, 0.698]; the starting adapter in this harness: 0.355 [0.285, 0.430]; before the Evolver's port onto the agent layers, 0.602 (four trainings), 0.616 (engineering weights) and 0.378 (the starting adapter) |

Paired per question, 95% bootstrap intervals, a method's four runs averaged per question first; bold where the
interval excludes zero; the Flash row's GLM intervals beside them:

| Difference | GPT-5.6-Luna | won / lost | GLM 5.3 Flash |
|---|---|---|---|
| initial skill − none | +5.2 [−0.6, +11.0] | 18 / 9 | +3.5 [−2.3, +9.9] |
| GEPA − none | **+29.2** [+22.1, +36.5] | 73 / 16 | +3.9 [−2.3, +10.5] |
| SkillOpt − none | **+23.8** [+17.6, +30.2] | 65 / 14 | +6.0 [−0.3, +12.4] |
| GEPA − initial skill | **+24.0** [+16.7, +31.1] | 67 / 17 | +0.4 [−5.4, +6.2] |
| SkillOpt − initial skill | **+18.6** [+12.1, +25.1] | 57 / 19 | +2.5 [−3.6, +8.6] |
| SkillOpt − GEPA | **−5.4** [−9.4, −1.5] | 26 / 44 | +2.0 [−1.0, +5.1] |
| Harness² deliverable − its reference execution | **+22.1** [+15.7, +29.1] | 41 / 3 | **+7.6** [+1.7, +13.4] |
| Continual Harness − Harness²'s reference execution | **+8.7** [+1.7, +15.7] | 28 / 13 | +5.8 [−1.2, +12.8] |
| ReHarness − Harness²'s reference execution | **+12.8** [+5.8, +20.3] | 32 / 10 | +5.8 [−1.2, +13.4] |
| Continual Harness − Harness² deliverable | **−13.4** [−20.3, −6.4] | 9 / 32 | −1.7 [−7.6, +4.1] |
| ReHarness − Harness² deliverable | **−9.3** [−15.7, −2.9] | 8 / 24 | |
| ReHarness − Continual Harness | +4.1 [−2.3, +10.5] | 19 / 12 | |
| RRSI Adapter − its starting adapter (same harness, same window) | **+26.2** [+20.1, +32.6] | 63 / 13 | |
| RRSI Adapter, engineering weights − its starting adapter (same harness, same window) | **+26.7** [+19.8, +33.7] | 49 / 3 | |
| RRSI Adapter − GEPA | +1.6 [−1.6, +4.8] | 32 / 27 | |
| RRSI Adapter − SkillOpt | **+7.0** [+3.3, +10.6] | 45 / 17 | |
| RRSI Adapter − Harness² deliverable | +0.6 [−4.2, +5.5] | 23 / 33 | |
| RRSI Adapter − ReHarness | **+9.9** [+4.5, +15.6] | 37 / 26 | |
| RRSI Adapter − Continual Harness | **+14.0** [+8.3, +19.6] | 43 / 20 | |
| RRSI Adapter, engineering weights − RRSI Adapter | +0.6 [−4.2, +5.5] | 35 / 21 | |
| RRSI Adapter − RRSI Adapter before the port | +1.5 [−1.9, +4.8] | 36 / 28 | |

The differences against no skill and the initial skill are a little high: those two evaluations lost 4 and 3
sessions to the login's rate limit (at most 2.3 and 1.7 points; by the cells' own rates about 0.7 and 0.6, by those
questions' scores in the other nine one-pass evaluations about 1.1 and 0.7). Continual Harness's and ReHarness's
gains over the reference execution overlap GLM's intervals: they cannot be called larger than GLM's.

- **Where the points go.** Every answer was checked one by one (2,408: 172 per cell, 688 for GEPA's and SkillOpt's
  four trainings together) for whether its value is right and whether its form is the one the judge accepts; RRSI
  Adapter's runs before the port were checked afterwards on their 517 answers that are not exact matches (317
  distinct), and its runs on the agent layers on their 440 (284 distinct; the 116 an earlier check had read, the same
  answer to the same question, kept its verdict); an exact match has the right value in the accepted form:

  | Cell | exact match | right value, other form | wrong value | no answer (timed out) | right value |
  |---|---|---|---|---|---|
  | no skill | 53 | 62 | 53 | 4 | 0.669 |
  | initial skill | 62 | 56 | 51 | 3 | 0.686 |
  | GEPA, four trainings | 413 | 53 | 222 | 0 | 0.677 |
  | SkillOpt, four trainings | 376 | 77 | 235 | 0 | 0.658 |
  | Continual Harness | 82 | 36 | 54 | 0 | 0.686 |
  | ReHarness | 89 | 26 | 57 | 0 | 0.669 |
  | Harness², deliverable | 105 | 17 | 50 | 0 | 0.709 |
  | Harness², reference execution | 67 | 55 | 50 | 0 | 0.709 |
  | RRSI Adapter, its starting adapter (same harness) | 61 | 56 | 55 | 0 | 0.680 |
  | RRSI Adapter, four trainings | 424 | 66 | 197 | 1 (the model's output limit, not a timeout) | 0.712 |
  | RRSI Adapter, engineering weights | 107 | 16 | 49 | 0 | 0.715 |

  Paired on the value alone, GEPA − initial skill is −0.9 [−5.7, +3.8], SkillOpt − initial skill −2.8 [−7.7, +2.0],
  Harness² − its reference execution 0.0 [−4.7, +4.7], Continual Harness − the reference execution −2.3 [−7.6, +2.9]
  and ReHarness − the reference execution −4.1 [−9.9, +1.7], RRSI Adapter (four trainings) − its starting adapter
  +3.2 [−0.1, +6.7] (before the port, +0.7 [−3.5, +4.9]; checked the same way, answer by answer, after the column):
  the learned adapters turn "right value, other form" into
  exact matches and little else. Two automatic readings (the reference answer at the start of the answer; anywhere in
  it) never pass a wrong answer but miss many right values (a `%` the reference answer has or lacks, 109 answers;
  list separators 23; a final period 22; trailing zeros 20; a U+2212 minus sign in the reference answer 17), hence
  the check by hand. Six reference answers contradict the form their question asks for (UID0160, 0225, 0089, 0013,
  0196, 0120): about 3.5 points no method can take. GLM writes the bare value by default: a GLM 5.3 Flash evaluation
  of the same initial skill in the same harness on this machine (run `flash-codex-initial-skill`, 0.564; not the
  Flash row's run) has 19 rejected answers that contain the normalised reference value, against 54 with
  GPT-5.6-Luna (56 by hand).
- **What was learned.** The initial skill says nothing about form ("Copy the final answer from a checked value, not
  from an unverified intermediate guess"). Afterwards the skill from GEPA's first training says "Output only the
  requested answer. Do not include explanations, labels, citations, Markdown, XML/HTML tags, JSON wrappers, ..." and
  the other three carry a line to the same effect ("Return only the requested answer ..."); 17 of the 21 harnesses
  in Harness²'s library carry such a line; Continual Harness's `AGENTS.md` says "Put only the normalized answer value
  and any required unit words inside the tags", the unit words leaving one answer in ten in a form the judge
  rejects; ReHarness's `ADAPTER.md` is unchanged and its advice is spread over the 172 retrieved reflections, at
  times contradictory. Bare-value answers: 94–98% of GEPA's, 79–94% of SkillOpt's, 83% of Harness²'s, 65% of
  ReHarness's, 52% of Continual Harness's, 37–44% in the three cells without a learned skill.
- **Selection.** SkillOpt's four `final` skills are its best by validation, from steps 2, 7, 4 and 1 (validation
  means 0.667, 0.708, 0.708, 0.667); GEPA's from 46, 46, 44 and 44 candidates. GEPA's four finals spread 0.581–0.622
  (sd 0.017), SkillOpt's 0.523–0.599 (sd 0.035): SkillOpt's best training (0.599) is above GEPA's worst (0.581), and
  over trainings SkillOpt − GEPA is marginal (p ≈ 0.05) where the per-question interval excludes zero.
- **Time and the login.** The first wave of trainings (`luna-gepa`, `-r2`, `luna-skillopt`, `-r2`) trained 11–15 h
  while the login was in a slow state (first replies a median 33–54 s, against 1.4–2.1 s afterwards) and learned
  from 0.8–2.1% timed-out rollouts; the second wave (`-r3`, `-r4`) from 0–3 timeouts; the eight evaluations of
  trained skills ran after the recovery with no timeouts. Two seed-validation rollouts of `luna-gepa` were lost to
  "Selected model is at capacity" and scored as wrong, before the agents learned to run such a session again. With
  the eight trainings side by side and no limit (about sixty sessions at once) the backend answered 429 Too Many
  Requests, first replies took a median 100–400 s and 32–45% of three successive batches' training rollouts timed
  out; those batches were discarded. The runs behind the results held every SkillOpt-harness rollout and every
  training learner call to one of a machine-wide number of session slots (`hermit/agent/base/login.py`, the file `SLOTS`), set by a
  controller (not in the repository) that cut two slots on timeouts and added one after quiet minutes; the slots
  kept the overload from returning but did not lower the timeout rate while the login was slow (4.4% at 2 slots,
  4.6% at 3–5): its recovery did.
- **Tokens, no prices** (the plan bills none; a session's count is Codex's input, cached included, plus output):
  3.4–4.0 M per 172-question evaluation; per training 4.1–4.5 M of training rollouts, 0.6–1.1 M of learner calls
  (GEPA 0.6–0.7 M, SkillOpt 1.0–1.1 M) and, the bulk of a training, the sessions the Evolver runs to score its
  candidates on the validation questions and its minibatches: GEPA 20–23 M, SkillOpt 7.2–7.7 M; per stream 46 M of
  sessions and 11 M of learner calls for Continual Harness, 29 M and 25 M for ReHarness, and for Harness² 27 M of
  reference executions, 83 M of candidate executions (three per question, the deliverable among them) and 197 M of
  editor sessions. All runs together took the plan's weekly window from 29% to 38% (whole percents, account-wide: an
  upper bound on their share); no session was refused for the plan's limit and the credits balance did not change.
- **Two defects of the ports**, found on this row and not fixed here, which touch the GLM row too:
  `utils/task-info/README.md` tells the learner the judge removes "unit words", where `normalize_answer` removes
  million, billion, dollar and nominal and keeps `%`, thousand and trillion; only Continual Harness's Refiner reads
  it, and it wrote "required unit words" into its policy (steps 2–172; "any required unit words" from step 5). The
  Continual Harness Evolver counts Codex's own notices (the hook-trust warning `codex_agent.py` always causes,
  reconnects) as failure signatures in every step, where upstream sends tool failures to its sub-agent and skill
  passes only; the Refiner never sees the acting prompt, and from step 84 its policy banned the note writing that
  prompt asks for (the agent wrote notes in 83 of 83 sessions before, 8 of 89 after).

### The Flash row with Claude Haiku 5.5

The no-skill evaluation once more, with Claude Code 2.1.318 and Claude Haiku 5.5 (effort medium) acting on a Claude
subscription instead of Codex on a ChatGPT login, in the same containers, and the Harness² stream set up to act **and**
learn the same way. The
[example's results](https://github.com/Infini-AI-Lab/hermit/tree/main/examples/officeqa#results) have the column and
what it shows; `skillopt/run_claude_haiku.yaml` and `harness2_stream/run_claude_haiku.yaml` are the luna configs with
the agent and the models changed, run with the commands in
[Run](https://github.com/Infini-AI-Lab/hermit/tree/main/examples/officeqa#claude-haiku-55-on-a-claude-subscription)
(the sign-in, the plan and the boundaries: [the guide](../guides/claude-subscription.md)). The data, the split, the
seed skill, the judge, the prompt, the task functions, the Harness² stream order, H0, its parameters and stage prompts,
the 3,600 s session clock and the one test pass are the luna column's. The runs below ran before the one sandbox design
(#78), under its keys at the time: the acting agent's `isolation: container` with `container`, today
`environment.image` and `environment.container`, and the rollout role's bubblewrap sandbox, today the task sandbox.
What differs:

| Setting | With GPT-5.6-Luna | With Claude Haiku 5.5 |
|---|---|---|
| acting agent | Codex 0.164.0 (`agent.base: codex`), a Docker container per session behind an egress proxy (chatgpt.com, auth.openai.com) | Claude Code 2.1.318 (`agent.base: claude`), a Docker container per session behind an egress proxy (api.anthropic.com): the image `officeqa-claude:2.1.318` ([Claude Code in the same containers](#claude-code-in-the-same-containers)) on the same network, with the same mounts and limits |
| the agent's tools | Codex's code mode (a JavaScript `exec` tool calling `exec_command`), no web search; the commands had no network | Claude Code 2.1.318's default set (Bash, Read, Edit, Write, NotebookEdit, Task for sub-agents, Skill, ToolSearch and the rest it ships; no Glob or Grep in this build: search goes through Bash), WebSearch, WebFetch and the tools whose work outlives a session denied (the no-skill sessions called Bash and Read only); the commands ran in Claude Code's own sandbox, as Codex's in its, and had no network. The sessions also carry Claude Code's 17 built-in skills and 5 built-in agents: "no skill" is no adapter, not no skills |
| sign-in and plan | `login: chatgpt`, a shared copy of the machine's Codex login; `~/.cache/hermit/codex-login/plan/` | `login: claude`, the token of `claude setup-token` in `CLAUDE_CODE_OAUTH_TOKEN`, to the container by name and to Claude Code on a file descriptor (no credential copied); `~/.cache/hermit/claude-login/plan/`, outside the containers, `MAX_USED` 90 during the runs |
| no skill | SkillOpt's former Codex harness, 180 s sessions re-run up to 24 times without an `<answer>` (0.308) | the framework's agent (`skillopt/run_claude_haiku.yaml --adapter none`), 3,600 s, one session: no session reached 180 s (the longest 166.5 s) and every one answered, so the old harness's cap and re-runs would not have changed a question |
| Harness² executions | Codex sessions, read by upstream's Codex renderer | Claude Code sessions, each in a container as above, running the same `harness2-codex` tree in its own places (`CLAUDE.md`, `.claude/skills/`, `.claude/agents/`, `.claude/settings.json`), read in upstream's grammar by `harness2_upstream/render.py`; Haiku's streams carry no thinking text, so the editors see no `[REASONING]` lines |
| Harness² editors | Codex sessions (`backend: codex`, `login: chatgpt`) | Claude Code sessions (`backend: claude`, `login: claude`: upstream's `claude_cli` channel with Read, Write, Edit, Glob, Grep and Bash), told of upstream's Codex surface as before; `slots_claude.yaml` names Claude Code and its file tools where `slots_codex.yaml` names Codex's shell |
| learner (`llm`) | `llm: {cli: codex, ...}`, one Codex session per call | `llm: {cli: claude, model: claude-haiku-5-5, login: claude}`, one Claude Code session per call that can only reply, in a bubblewrap sandbox of its own (`isolation: container` gives it the image and proxy above); no number below used it: the column is the no-skill evaluation, and the one learner call checked ran in a container |
| run names | `luna-*` | `haiku-none` (`skillopt/runs/`), `haiku-h2-s0` (`harness2_stream/runs/`) |

**No skill.** One pass (`--adapter none trainer.run_name=haiku-none`, 2026-11-05, every session in a container of its
own): **0.599** [0.523, 0.669], 103 of 172, no error. The 172 sessions took 427 s, 8 at a time (13.7 s each at the
median, 166.5 s at the longest, 4 turns at the median); they called Bash 637 times and Read 9 times, no other tool, and
used 24.9 M tokens (20.5 M read from the prompt cache, 3.9 M written to it, 0.5 M of output; 97 K a question at the
median). Claude Code's sandbox adds a section to each session's first message (how it is set up, and that a task that
cannot be finished with what was given should say what is missing) and to the Bash tool's description: a session's first
request was 18.9 K tokens at the median, against 15.6 K without it. Every session ran on the login
(`apiKeySource: none`) and on Claude Haiku 5.5 alone (the CLI's `init` and `modelUsage`), none met a limit or drew usage
credits, and no record holds the token. Three earlier passes, on earlier versions of this change, scored 0.640 [0.570,
0.709] in containers, before the review's last fixes (they change a session's first request by one number), and 0.622
[0.547, 0.692] and 0.634 [0.558, 0.703] in the rollout role's bubblewrap sandbox instead of a container, whose sessions
were offered tools a session is now denied (all seven in the 0.634 pass, `ListAgents` and `SendMessage` in the 0.622
one). The four average 0.624 (standard deviation 1.8 points). This pass less each, paired: −4.1 [−9.3, +1.2],
−2.3 [−7.6, +2.9] and −3.5 [−8.7, +1.7]; the two passes in containers less the two without, per question: −0.9
[−4.1, +2.3]. 89 questions are right in all four passes and 47 in none. The isolation moved the score no more than
another sample does, and the samples differ in the answer's form, not in its value (below).

**After the one sandbox design.** The same evaluation on this code after #78 (the acting agent's container from
`environment.image` and `environment.container`, the learner in a bubblewrap sandbox of its own; 2026-11-07, run
`haiku-none`): **0.616** [0.547, 0.686], 106 of 172, no error, 413 s at 8 at a time. Every session ran on the login
and on Claude Haiku 5.5 alone, none on usage credits; Bash 655 and Read 9 calls; 26.0 M tokens (21.5 M read from the
prompt cache; 88 K a question at the median); Claude Code's sandbox refused 13 connections to data hosts, the
egress proxy let 519 through to `api.anthropic.com` and saw no other host; no record holds the token. Paired with
the four passes above: +1.7 [−3.5, +7.0] against 0.599, −2.3 [−7.6, +2.9] against 0.640, −1.7 [−6.4, +3.5] against
0.634, −0.6 [−5.8, +4.7] against 0.622, and −0.7 [−4.9, +3.5] against their mean per question; the five average
0.622 (standard deviation 1.6 points). Its misses were not checked by hand.

- **Where the points go.** The 69 misses were checked one by one, as the luna column's answers were (31 are the answer
  an earlier pass gave to the same question, and kept its verdict):

  | Cell | exact match | right value, other form | wrong value | no value | right value |
  |---|---|---|---|---|---|
  | Claude Haiku 5.5, no skill | 103 | 20 | 44 | 5 (the session said the corpus lacks an input) | 0.715 |
  | GPT-5.6-Luna, no skill (above) | 53 | 62 | 53 | 4 (timed out) | 0.669 |

  The 20 are a `%` the reference answer has or lacks (7), words or units around the value (6), trailing zeros (3), a
  percent where the question asks for a decimal (2), a list separator (1) and a value not rounded as asked (1); four of
  them (UID0160, 0225, 0089, 0120) are among the six reference answers that contradict the form their question asks for.
  Both passes in containers have the value right in 0.715 of the questions (the 0.640 pass with 110 exact matches and 13
  in another form), and the 0.622 pass, checked the same way, in 0.703: the passes differ in how often the value comes
  in the judge's form, not in how often it is right. Without a skill Claude Haiku writes the value in the form the judge
  accepts far more often than GPT-5.6-Luna, and has the value right a little more often (0.715 against 0.669, not
  paired): its 29 points over luna's no skill are almost all the form, as the luna column's gains are.
- **What the isolation refused.** The sessions' commands tried 14 connections, all for data the corpus lacks: FRED (7),
  BLS (3), the World Bank (2), FRASER and Macrotrends. Claude Code's sandbox refused each inside the container, so none
  reached the egress proxy, which let through 517 connections, all Claude Code's own to api.anthropic.com, and refused
  none. The five sessions with no value each said which input they could not find or trust (a chart only in an image,
  the January 2003 Aa yield, a 1968 gilt yield, 1961's unemployment rates, a 1959 exchange rate) instead of answering
  without it.

**Harness².** Not run in full. Its stream (run `haiku-h2-s0`, on this change before its review, with the executions in
the rollout role's bubblewrap sandbox: before the containers) was started and stopped during its third question, with
no summary. Its first two questions ran end to end: every stage on Claude Code sessions
on the login, no stage falling back and no editor failing, and in the second the deliverable's composed skill deployed
in `.claude/skills/` and called by its execution through Claude Code's Skill tool. A two-question smoke before it
(`MAX_TASKS=2`, run `haiku-h2-smoke`) ran on a still earlier version, whose sessions had all of Claude Code's tools. The
column has no Harness² cell.

## Included adapters

`examples/officeqa/gepa/adapter/` and `examples/officeqa/skillopt/adapter/` are the skills `gepa/run.yaml` and
`skillopt/run.yaml` trained in direct chat. `examples/officeqa/ace/adapter/` is the `final` playbook of
`ace/run_codex_luna.yaml`'s measured run ([ACE](#ace)), its card with the run's settings and test score. Evaluate them with a
directory selector:

```bash
hermit eval --config examples/officeqa/gepa/run.yaml --adapter adapter
hermit eval --config examples/officeqa/skillopt/run.yaml --adapter adapter
hermit eval --config examples/officeqa/ace/run_codex_luna.yaml --adapter adapter
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
