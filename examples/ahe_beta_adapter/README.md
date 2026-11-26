# AHE-Beta Adapter

**Agentic Harness Engineering Beta: Requirement-Centered Evolution of Agent Harnesses**
([arXiv:2611.24924](https://arxiv.org/abs/2611.24924), [code](https://github.com/Curry09/AHE-Beta)) evolves a coding
agent's harness from what simulated users ask for while they work with it: ❶ a user simulator follows a persona
through multi-turn sessions (follow-ups, mid-run messages, interruptions, its own checks) and files issues and a 1–4
rating; ❷ a team (Triage, Architect, PM, Developers) turns the issues into requirements and the requirements into
general changes; ❸ a change merges only when it resolves a verified requirement and passes review. No verifier result
or benchmark score is used during evolution.

AHE-Beta evolves its whole NexAU seed harness (its configuration and entry point, tool definitions, prompts and
parameters). **The AHE-Beta Adapter is AHE-Beta's loop confined to an adapter**, as the [AHE Adapter](../../docs/methods/ahe_adapter.md)
is AHE's: Codex, its loop and its tools are fixed, and only the adapter changes (`template/`: Codex's project
instructions, command hooks, helper scripts, skills and roles); the user simulator, the tasks and their verifiers stay
fixed too. AHE-Beta's own Hermit integration makes the same choice (its handoff to the delivery team names "an external
Hermit Adapter deployed to an unchanged host" as the evolving product). What an adapter cannot express (a change to
Codex's loop, its native tools, its context handling or its sampling) is out of reach, so results are not comparable
with the paper's numbers; the [method page](../../docs/methods/ahe_beta_adapter.md) has the comparison.

| Route | Acting | User | Learning | State |
|---|---|---|---|---|
| `run.yaml` from [`prepare.py`](prepare.py) | Codex in each task's Harbor environment ([`utils/user.py`](utils/user.py)) | AHE-Beta's own simulator ([`utils/ahe_user.py`](utils/ahe_user.py)), its model through Hermit | Hermit's Codex Evolver on the users' verdicts | run (Validation, below) |
| `run-delivery.yaml` from [`bootstrap.py`](bootstrap.py) | the same | the same | AHE-Beta's own demand and delivery pipeline ([`delivery/`](delivery/): Triage, Architect, PM, admission; OpenAgents developers on a Gitea repository), in the run's sandbox | run end to end once (Validation), not evaluated |
| [`smoke/run.yaml`](smoke/run.yaml) | the same | a scripted user ([`utils/scripted_user.py`](utils/scripted_user.py)) | none (`hermit eval`) | run (Validation) |

The simulated user lives in this example for now. Its contract, `simulate(task, agent, workspace, params)`, is the one
a Hermit abstraction will keep when it moves into the framework (with real people giving a rollout feedback and
interrupting it); a simulator written against it should not need to change then.

## The user's contract

```python
def simulate(task: dict, agent: Conversation, workspace: Workspace, params: dict) -> dict
```

| Argument | What it is |
|---|---|
| `task` | `{"id": the row's id, "name": the Harbor task's name, "instruction": its instruction.md}` |
| `agent` | the running Codex session ([`utils/channel.py`](utils/channel.py)): `write(text)` (the first message starts Codex with it as the prompt; a later one reaches the model after the running tool call, or as the next turn), `poll(timeout)` (the next reply `{text, interrupted}`, or None while it works), `send(text, timeout)`, `events()` (tool_start, tool, denied, delivered, adapter_block, interrupted_reply, timeout), `interrupt(text, timeout)` (tool calls are denied until it stops; a running tool is not cancelled; the reply it stops with, or None, and then that reply comes as an `interrupted_reply` event, never as the answer to a later message), `close()` |
| `workspace` | the task's environment as the user reaches it ([`utils/user.py`](utils/user.py)): `run(command, timeout, cwd)` → `{code, stdout, stderr, timed_out}` as the task's agent user, `read`, `write`, `quiesce()` (the agent stopped and what it started ended, so a review sees frozen files); a task service's `service_run` / `service_read` in the main container |
| `params` | `user.params`, the task's assignment (its persona, scenario, round: `user.assignments` or the row's `user.task_field`), `session_id`, `artifact_root` (a private folder of this machine), `adapter_sha256`, `plan_admitted` (the run holds the login's plan admission for the task's environment) |
| returns | `{"score": a number in [0, 1], "rating", "outcome", "reason", "issues", "feedback", "usage", "files", "record", ...}`; `usage.tokens` and `usage.cost_usd` (None for a model without a price), when given, are added to the rollout's; an exception whose `final` is true is a failure no run again mends |

## How a session runs

One `codex exec` runs for the whole conversation, in the task's own Harbor environment, through Hermit's Codex agent
(`UserCodexAgent` subclasses `hermit.agent.acting.workspace.CliAgent`: the adapter's deployment, the sign-in, the
stream, the usage and the infra classification are the framework's). Three hooks ([`utils/user_hook.py`](utils/user_hook.py))
carry the conversation through a bridge ([`utils/channel_server.py`](utils/channel_server.py)), both installed as root
outside the workspace before Codex starts: PostToolUse inserts a waiting message, PreToolUse denies tool calls after an
interrupt, Stop hands the reply to the user and continues the session with the user's next message. The adapter's own
hooks stay; its Stop hooks run inside the user's Stop hook first (Codex runs one event's hooks at the same time), so
they must be command hooks without a matcher (the template's check refuses others); they run one after another, the
first that blocks or ends the session deciding (the later ones are tried again at the next Stop, after the earlier
ones). This machine talks to the bridge
with one command at a time in the environment (a read waits there up to 5 s for new events), so nothing is mounted from
this machine and the agent's writable workspace holds no queue. Codex's clock ends `user.review_s` (600 s) before the
task's deadline (taken when Codex starts, so that the channel's setup and the user's first turn come off Codex's time),
so that the user's last turn and review fit in it. The session ends with the user's end, then
[`utils/processes.py`](utils/processes.py) ends whatever the rollout started (the bridge and the task's registered
services stay): AHE-Beta's freeze before collection and verification, so a task whose tests need a server the agent
left running is graded differently than Harbor grades it. The bridge and the hooks need python3 3.8 or later in the
image.

The channel is out of the agent's way, not out of its reach: the bridge, its hook and `.codex/` are root's and
read-only, and the bridge answers a process whose command line, or an ancestor's, names the hook and Codex (a check of
`/proc` text, not of identities), which every command the agent runs under Codex can satisfy (AHE-Beta's exported
images also run the agent as root). An agent that looked for it could take a queued message or report a reply or a
tool event the user then reads, and the user's verdict rests on what it read: the judge reads the verdict this machine
kept, never a file in the environment, but what the user observed came through a channel the agent can reach, as the
paper's own transport does.

What the run places in the workspace (the hooks, the adapter's deployment: `.codex/`, `AGENTS.md`,
`.hermit-deploy.json`, `hermit/<template>/`) is listed in the task repository's local exclude file (`.git/info/exclude`)
while the session runs, when the repository does not track it, and taken back file by file once the agent has
stopped, before the user's review and the task's own capture: each placed file removed and what it overlaid put back as
it was (a task's own `AGENTS.md`, which the deployment prepends the adapter to, a link as a link), each folder the
run made removed once empty (with a deployed helper's bytecode); the agent's own files beside them stay, but not an
edit of its own to a file the run
overlaid (a task that asks for a change to `AGENTS.md` loses it). Neither AHE-Beta's prediction patch nor a finalizer
that archives the whole workspace holds any of it, and a workspace that could not be put back is never reviewed (the
session fails). A tool's input and output reach the user whole up to 20,000 characters each, and a reply up to 50,000,
else cut. Once the user ends the session, every tool call the agent still tries is denied and no queued message is
handed over; so is a tool call while the channel cannot be read (an end may be waiting there). After an interrupt that
does not wait (AHE-Beta's), the stopped turn's reply comes as an
`interrupted_reply` event (a stop reply names the interrupts it answers, so an interrupt that does wait gets the reply
it shares with an earlier one); AHE-Beta's own handle closes a request only on a reply, so `ahe_user.py` has its
interrupt end the request it stops (otherwise its native records counted the interrupted request and the next as one).
An adapter's Stop hook that ends the session (`continue: false`) is recorded by AHE-Beta's handle as a completed
request, and a message the user writes after it reaches AHE-Beta's user loop as a harness error (the agent is gone),
which it shows the persona. The exported
tasks declare no network, so the agent's phase has the public network (Harbor's default for a task that says nothing),
as AHE-Beta's E2B sandboxes do: they require internet access.

What a run keeps, and what a learner reads:

| Where | What | A learner reads it |
|---|---|---|
| `log/user-transcript.jsonl` | every message, reply and tool event the user could see, with times | yes (the trajectory) |
| `log/user-verdict.json` | score, rating, outcome, reason, issues, feedback | yes |
| judge feedback ([`utils/judge.py`](utils/judge.py): `user`) | the rating, outcome, reason and issues as text | yes |
| `<workspaces>/user/<session>/` | what the simulator wrote (AHE-Beta's native session: its record, the persona, its private checks) | no (never in the log; AHE-Beta's own Evolver hands it to its controller) |
| `<workspaces>/user/<session>.hermit/` | the verdict's files and record (AHE-Beta's full outcome among them) | no |
| `assignments.json` | each row's persona, scenario and round (`prepare.py`) | no: the rows hold only id, Harbor task and instruction |
| the task's verifier | the run takes its verdict anyway and records it with the rollout (`pipeline_extra.verified`) | yes: harmless for AHE-Beta's exported tasks, whose verifier only copies the user's score; train only on such tasks, since a task set with real tests would show the learner its verdict, which AHE-Beta's protocol does not allow (the judge `user` keeps it out of the score and the feedback) |

A session whose user failed (the simulator raised or gave no valid score) is not the agent's result: `extra.infra =
user_failed`, which the run runs again in a fresh environment (`environment.harbor.infra_retries`) and, failing again,
records as a failed run (`judge_error`), not as the agent's 0, though the means count it as 0 (`hermit eval`'s
`mean_score`, a step's `mean_score` in its learning material and in `steps.jsonl`). It is not run again when no run
could do better: an image without python3 3.8 (a probe that failed otherwise is run again), a task that needs a
companion container, a user model that cannot serve as configured (a key not set). So is a Codex session that was
infra. A session that ran out of its time before the user's verdict is the agent's result, score 0, as AHE-Beta rates a
session that delivered nothing: Harbor ended the agent's phase while it ran, the deadline had passed before the session
was stopped, or Codex's clock ended the session and the user then found it gone. A failure of the user's own after the
session was stopped in time (its review, its capture, its model) stays the user's. A failure to stop the session after
the user's verdict leaves the verdict (`extra.user_cleanup_error`). An adapter that failed before Codex started is the
agent's result too (0).

## Setup

```bash
python -m pip install -e '.[test]' -r examples/ahe_beta_adapter/requirements.txt   # Hermit's environment: the simulator runs here
uv venv --python 3.13 ~/.cache/hermit/harbor-venv && uv pip install --python ~/.cache/hermit/harbor-venv/bin/python harbor==0.25.0
export AHE_ROOT=~/src/AHE-Beta                  # where AHE-Beta's checkout goes (any folder outside the cohort)
git clone https://github.com/Curry09/AHE-Beta "$AHE_ROOT" && git -C "$AHE_ROOT" checkout 43b165fff7b01e01f4fe4697b7597ccd61700727
codex -c 'cli_auth_credentials_store="file"' login   # the ChatGPT login the configs use, in the file Hermit copies
export HERMIT_CODEX_RELEASE=~/.codex/packages/standalone/releases/0.170.0-x86_64-unknown-linux-musl
```

The Codex CLI in each task's container is this machine's static release, mounted read-only at `/opt/hermit-codex`
(`environment.harbor.environment.mounts`) and named by `agent.codex.bin`: Hermit asks only its built-in agents what to
install in a task's environment (hermit.isolate), and `UserCodexAgent` is this example's class. Its version must match
`agent.codex.version` (`prepare.py --codex-version`). Each task needs `python3` in its image for the bridge.
`prepare.py` refuses a machine whose Git ignores a file of the template (a global excludes file, `.codex/` say): Hermit
makes the adapter's tree with `git add -A`, which would leave it out of every adapter the run deploys.

## Train and evaluate

Export a cohort with AHE-Beta's exporter (its tasks as Harbor task directories, each round and slot one row, the
original persona schedule), then write Hermit's configuration beside it:

```bash
PYTHONPATH="$AHE_ROOT/backend" python -m ahe_beta.integrations.hermit.export \
  --experiment "$AHE_ROOT/examples/experiments/u8-mixed-e2b.json" --output <cohort> --cache <material cache>
python examples/ahe_beta_adapter/prepare.py --cohort <cohort> --ahe-root "$AHE_ROOT" --run-id u8-codex
cd <cohort>
hermit check --config run.yaml
hermit train --config run.yaml
hermit eval --config run.yaml --adapter final      # the last round, same cohort (AHE-Beta's R6), user-rated
```

One training step is one round (`step_size`: a round's sessions, `shuffle: false`, one epoch); the final split is the
cohort's last round, a same-cohort observation, not a held-out benchmark. The agent's budget is the exporter's
(`--agent-seconds`, which it writes into the tasks and the sessions' assignments alike; each task's own without it), and
`prepare.py --review-seconds` (600 by default) what of it Codex leaves the user's last turn and review: a cohort whose
budgets would leave Codex less than 300 s is refused. The models default to GPT-5.6-Luna on the ChatGPT login for the
actor and the user and GPT-5.6-Luna for the learner (`--actor-model`, `--user-model`,
`--learner-model`: a Codex model name, or a `<backend>/<model>` reference such as
`fireworks/accounts/fireworks/models/glm-5p3-flash`, the paper's model family). The user's model on a login is a chat
endpoint over Codex (hermit.agent.base.endpoint), each request a Codex session in a bubblewrap sandbox of its own that
reaches only the login's hosts (hermit.isolate.cli_sandbox). When the actor is on the same login (prepare.py then sets
`shared_plan`), those sessions count as their rollout's on the login's plan (the run admitted the task's environment:
they take no slot of their own); otherwise each waits for the plan and takes a slot as any session does. A reference is
reached through a local relay (hermit.agent.base.endpoint.relay), refused before a session when its backend needs a key
that is not set, its binding sets `extra_body` (which the relay cannot add to AHE-Beta's requests), or its endpoint
refuses the key (401) when asked for its model list before a process's first session (no model call, no redirect
followed with the key; a 403 may refuse the list alone, and a session finds out): otherwise
each session's review met the refusal after its actor ran, and the session ran again. Its endpoint must also take
AHE-Beta's terminal review as AHE-Beta sends it, once and unretried: `max_tokens: 32768`, and for a model other than
GLM-5.2 or GLM-5.3 given no effort, `enable_thinking: false` in the body (Fireworks' GLM-5.3 takes both; an endpoint
that refuses either, or caps output below that, fails every review). On a task whose repository
tracks `AGENTS.md`, the user's live view of the workspace shows what the deployment added to it until the deployment
is put back, before the review. The user model is
part of the rollout, and so is its spend: a session's tokens and cost add the user model's (AHE-Beta's usage of the
session, each call as it reported, priced from the reference's binding; no cost on a subscription login, as for the
actor) and `trainer.budget` counts them with the learner's; the actor's own are kept in `extra.actor` (the paper's
"Tokens" are the actor's). As Hermit prices any run, the session's cost is unknown (None, which an evaluation counts
as an unknown cost) when a part of it is: the actor's or the user model's without a price, or a user model's call
that reported none or part of its usage (`extra.user_cost_unknown`; its tokens still count, at least those reported). A
session whose user failed counts what its user model had spent all the same (AHE-Beta's usage ledger of the session),
and so does one that Codex's side failed (in what the failed run spent, which the run's budget counts).

### AHE-Beta's own Evolver

[`bootstrap.py`](bootstrap.py) provisions the Gitea repository from `template/`, a delivery clone and an OpenAgents
worker clone (configured as AHE-Beta configures its worker: its own roles triage, the CI and end-to-end stages are
skipped, each delivery shows its execution evidence), then has `prepare.py` write `delivery.json`, `run-delivery.yaml`,
`rounds.json` and `delivery-binding.json`: the seed adapter bound to the delivery clone's commit, which every session of
round 1 carries (AHE-Beta's feedback import requires it). The controller runs on AHE-Beta's own environment, the
developers on Claude Code (`claude` on this machine's PATH, run as a user other than root: as root Claude Code refuses
to run its tools unattended); the Evolver's setup checks these (AHE-Beta's interpreter a virtual environment's own,
started in the controller's view importing its entry point and NexAU; the OpenAgents pin), the enforced sandbox, the Gitea token, a Claude Code credential for the developers, the demand
roles' model and each process's folders (apart, none holding the cohort; AHE-Beta's environment too) before the run
pays for a round of sessions. The controller and the delivery run's sessions run AHE-Beta as the commit the run's
identity names (`controller_sha`): `prepare.py` writes its tracked code, role definitions and pins
(`backend/compatibility.json`, which choose the OpenAgents commit) to `ahe-beta/<commit>/` in the cohort, so that
neither a later change to the checkout nor its own untracked files (the simulator's `backend/ahe_beta/user/.env`,
which its persona client loads) reach them; each step checks the OpenAgents checkout is
still at its pin and unedited (the worker is shown it whole, its ignored files too: give it a checkout of its own,
`bootstrap.py`'s default). A folder `worker.readable` shows the worker is shown whole: give the file or folder a
wrapper needs, never one holding a credential (`~/.config` with the filled `experiment.env` in it, say), since the
developers run model-written commands. No folder the worker is given or shown (`worker.readable` too) may hold or
overlap the sessions' private records (the cohort's `.logs`, and at each step the stores the round's sessions used),
nor the Trainer's `runs`; a session rated without its native record (only one rated out of time has none) stops the step
rather than leaving its evidence out. Setup also runs the developers' `claude --version` in the worker's view of the
sandbox. Every round runs that file, checked again in that view at each step: its path, digest and version are kept
beside the binding (`delivery-developers.json`), so an update between rounds stops the step instead of changing the
developers. Upstream pins Claude Code 2.1.284 (its `backend/compatibility.json`, with the Linux binary's digest):
install that one for a reproduction; the record says whether it is. The delivery state belongs to one Trainer run (its
claim, with the delivery configuration's digest, is in the Evolver's state folder, which a restart keeps): a run
started over (`resume_mode=disable`) on one an earlier run used is refused at setup (bootstrap a new cohort instead),
and so is a step whose `delivery.json` changed since setup. The Gitea
is one of your own, reached at the URL given
(`--gitea-url`: the seed, both clones and the Evolver's mirror use it, not the one Gitea's API advertises; over https,
or on this machine as localhost or 127.0.0.1): AHE-Beta's Hermit controller cannot read Gitea.com's attachments back,
and the Evolver refuses it.
`bootstrap.py` refuses a cohort already delivered on (`run-delivery.yaml`), folders that overlap or hold the cohort
(the delivery clone, the worker's clone and its home beside it, the OpenAgents checkout, the controller's state), a
repository, clone or worker's home it did not make itself (it notes what it makes in the cohort's `bootstrap.json`
before making it, the repository by a marker of the cohort's own in its description, and a retry takes those again):
an earlier cohort's open issues would be this one's (AHE-Beta's triage takes every open issue), Git run here in a
clone a team wrote could run that team's configuration, and an earlier cohort's home would start the developers from
its files. It also refuses, on a cohort prepared before, a model or Codex setting
(the run's own are the delivery's) and a run.yaml that starts from another adapter than the bundled template (the
seed its repository and binding hold). `run-delivery.yaml` keeps run.yaml's schedule, which `prepare.py` writes as
one logical round a step, unshuffled, one epoch: AHE-Beta's controller takes one round a step, so keep it. The worker
runs as many developers as the exported experiment names
(`cohort.json`'s `roles.developer`, upstream's `vigil --parallel`), unless `--developers` says otherwise. Fill a copy of
[`experiment.env.example`](experiment.env.example) (the Gitea token, the demand roles' models, the developers'
Anthropic-compatible endpoint), keep it outside the checkout (it holds live credentials) and source it before
`bootstrap.py` and `hermit train`:

```bash
(cd "$AHE_ROOT" && UV_PROJECT_ENVIRONMENT="$AHE_ROOT/.venv" uv sync --locked --extra experiments)   # its own venv
. ~/.config/ahe-beta/experiment.env                          # the filled copy, chmod 600
python examples/ahe_beta_adapter/bootstrap.py --cohort <cohort> --ahe-root "$AHE_ROOT" \
  --ahe-python "$AHE_ROOT/.venv/bin/python" --run-id u8-ahe \
  --gitea-url https://<gitea host> --gitea-owner <owner> --gitea-repo <repository>
cd <cohort> && hermit train --config run-delivery.yaml
```

AHE-Beta's team is the paper's learner (its Algorithm 1: Triage, PM, Architect, Developers after each round's
sessions), so it runs where a learner runs. Its Architects run ungrouped, as in every configuration upstream ships (an
experiment of its own with `policies.architect_grouping: true` is not reproduced here). Each step of the Evolver ([`delivery/evolver.py`](delivery/evolver.py))
starts the OpenAgents worker (`openagents vigil` in its clone, from the pinned checkout's environment) and AHE-Beta's
controller (its own Python), each in a view of the run's sandbox that shows nothing of the run's own folders (its
adapter, material, history and output); neither starts without an enforced sandbox. Each is shown only its own folders
and handed only the variables named for it: the worker its clone, its home, its checkout (with the development extra
its reviews test with, its `python` first on the developers' PATH), `claude` and its own files (on PATH, for the
developers), and its configuration as this machine keeps it (read-only: a round's developers
cannot change the next round's); the controller AHE-Beta's code and role definitions (not the checkout's experiments,
persona library, secrets file or the records of its standalone runs), started outside the developers' tree, the round's
native session records and the delivery state and clone. The developers run with every tool allowed and commit as
`openagents`, as AHE-Beta runs them (`OPENAGENTS_PERMISSION_MODE`, `bypassPermissions` unless `worker.env` names and
sets another: in the default mode Claude Code neither commits nor pushes, and a ticket waits for a person). Neither process reads the cohort's `assignments.json` (the
personas) or this machine's home; the network is the sandbox's (`sandbox.evolver.allowed_hosts`: the Gitea host, the
hosts of the model variables set when `bootstrap.py` ran, `api.anthropic.com` when no `ANTHROPIC_BASE_URL` is set, and
`--delivery-host`), a Gitea on this machine's loopback forwarded into it (a loopback name is never on the proxy's list,
which would open every local port), as is a model on this machine's loopback that the handed variables name when a
step starts. The developers
learn of the sessions what the controller publishes to Gitea, as in AHE-Beta: each user's issue, which ends with
AHE-Beta's `_auto-collected` line for their triage (the persona's id, capability tier and error-correction style). That
line is kept as upstream sends it, by the maintainer's decision: it is the one part of a persona the team sees (the
validated route cuts it from what its learner reads). A session rated out of its time here, which left no native
record, keeps its rating but gives the team no evidence. The Evolver runs nothing the team can write: it takes the
delivered adapter from Gitea itself (a mirror of its own, the commit checked on main), copies back only the files the
template's contract covers as Hermit's tree keeps them (its `git add -A` and `git archive`: a file this machine's Git or
the delivery's own `.gitignore` files ignore, and the delivery's `.gitattributes` files, are left out, and the step's
summary names them; the commit of what it keeps, the delivered commit's child without any `.gitattributes` (an archive
of the delivered one would apply its root's too), is then pushed on main and bound, so that AHE-Beta's roles, which
read the bound commit back, see the files the sessions ran; made from the delivered commit's own time, a replay makes
the same commit), checks them with
`check_spec` (refusing a key's value, which the Trainer would scrub, and the Gitea token under any name), has a Git in
the sandbox move the team's clone to the bound commit, and writes the binding last, unless the Trainer stopped the
call meanwhile. A step that cannot deliver fails with what the team spent (its demand roles' calls in AHE-Beta's usage
store, from where the round's first attempt found it, each as it reported, and the developers' as unreported once the
worker started); the Trainer keeps the adapter, and the
next round's sessions run the last delivered one: the binding keeps each round's commit, and a session takes the
latest one, up to its round, that holds its adapter. A round AHE-Beta sealed in an attempt the Trainer never recorded
(this machine stopped before the Trainer's checkpoint) is taken again from its receipt or, when the stop came before
the receipt, its recovery record, nothing of the team rerun.
[`openagents_launcher.py`](openagents_launcher.py) remains for looking at a worker by hand; it refuses `vigil`, the
worker itself.

Run end to end once, on one round (Validation). Known limits: the
developers' usage (Claude Code in the worker) is not counted, each step records it as unreported (the budget's
unknown-cost calls), and the demand roles' calls are counted in tokens with no price (their model is no Hermit
backend: a dollar budget leaves both out); a delivery the step refused stays on the repository's main, where the next round's team builds on
it; a step this machine stopped while AHE-Beta's controller ran (a round frozen, not sealed) fails when redone, upstream
refusing the redone round's new sessions, and the run goes on with the next round; the mirror fetches main with its
whole history each step (a large file committed there once is fetched again).

## Benchmarks

| | Paper | Here |
|---|---|---|
| Evolution | 32 seeds (Appendix B): SWE-bench 9, Terminal-Bench 8, mechanism tasks 8, NL2Repo 4, TUA-Bench 3, each with a persona (15 cards of a 50-card library), 25 with interaction and 7 without; U8, U16 and U32 take the first 8, 16, 32; six rounds | the exporter's cohorts (`u8-mixed`, `u8-direct`, `u16-mixed`, `u32-mixed`), as Harbor task directories with AHE-Beta's images (`ghcr.io/mmjzmy/ahe-beta-tasks`) and inputs (its release `evolution32-task-materials-v1`) |
| Held-out evaluation | 38 Harbor-index tasks ([harbor-index](https://github.com/harbor-framework/harbor-index) `35f01ec`, release 1.4, AHE-Beta's `published38`): Coding 14, Optimization 12, Analysis 7, Tool Use 5; pass@1 by the tasks' own verifiers, user rating by one fixed evaluation persona that sees replies and a read-only diff, tokens per task | not packaged yet: in Hermit it is a Harbor task set (`environment.harbor`) judged by `utils/judge.py:verified` (pass@1, the user's rating beside it); an evaluation user that only reviews completed replies is a `simulate` of its own |

Tasks with a companion container (AHE-Beta's `kv-live-surgery`, in every u8, u16 and u32 round, and
`payments-pipeline-fix`, in every u32 round) need `service_run` there, which a Hermit environment does not reach yet:
their sessions would run the whole conversation and fail at AHE-Beta's finalize. `prepare.py` leaves them out
(`left-out.json`), so a step is a round's remaining sessions: 7 in u8, 15 in u16, 30 in u32. A session of one that
reaches the acting agent anyway fails for good (`user_failed`, final).

## The authors' implementation against this one

| Dimension | AHE-Beta | Here | Why |
|---|---|---|---|
| The harness learned | NexAU's whole seed harness (configuration, entry point, tool definitions, prompts, parameters) | a Codex adapter: instructions, skills, scripts, roles, hooks; Codex's loop, tools, context handling and sampling fixed | the framework: only the adapter changes; the reason for the name |
| Tasks | 32 seeds; U8, U16 and U32 over six rounds | the same cohorts, without the sessions whose task needs a companion container | a Hermit environment does not reach a companion container yet |
| User simulation | its persona loop, goal ledger, private checks, 90 s decisions, 24 turns, terminal review | the same code (its checkout's `ahe_beta.integrations.hermit.session`), its transport Hermit's channel | |
| What the learner reads | issues, sessions' interaction records and harness logs (Triage), ratings and summaries (PM) | the users' verdicts (rating, outcome, reason, issues) and transcripts, the agent's stream; no persona; a verifier's verdict only as the exported tasks' copy of the rating | the boundary of the learning material (above) |
| Modification and selection | four roles on Gitea, admission, RFC, review, merge | `run.yaml`: one Codex learner, the protocol in the template's learner guide (`.hermit/EVOLVER.md`), `check_spec`; `run-delivery.yaml`: AHE-Beta's own, in the run's sandbox | the validation route uses Hermit's Codex Evolver |
| Models | GLM-5.3-Flash acting and user, GLM-5.3 team, Claude Code developers | any: GPT-5.6-Luna on a ChatGPT login by default; GLM 5.3 Flash by reference | |
| Runtime | E2B sandboxes, AHE-Beta's controller | each task's Harbor environment (local Docker by default), Hermit's Trainer | |
| Not ported | the held-out evaluation suite, its evaluation persona, the human A/B study | | |

## Validation

Run on 2026-11-24 to 2026-11-26 with Hermit at this branch, Harbor 0.25.0 (local Docker), Codex 0.170.0 (the static
release, mounted), AHE-Beta at `43b165f`, every model GPT-5.6-Luna on a ChatGPT login unless said (the learner
GPT-6-Sol, medium effort; the actor medium, the user low). These check that the route runs end to end on real models;
they are not the paper's experiment and measure no effect.

| Run | What it checked | Result |
|---|---|---|
| `smoke/run.yaml` (`hermit check`'s probe and `hermit eval`), the scripted user | a message inserted mid-run (delivered at PostToolUse, acted on), a follow-up (delivered at Stop), an interrupt (the next tool call denied, the reply marked interrupted), a final question, the review of the workspace | 2026-11-24, no adapter: 6 of 6 checks, 76 s. 2026-11-25, after the dual review's fixes, the template deployed as the adapter (`--adapter ../template`, relative to the config) and eight checks tied to what the channel did (each message's delivery site by its id, the denied call for `four`, the final reply against the file in order): 8 of 8, 77 s |
| AHE-Beta's simulator on `010-pivot-product-revenue` (`hermit check`'s probe of an exported u8 seed, no adapter) | the persona's own first request, the session, the terminal review, the native session record kept privately | rating 4, 286 s; 4 user-model sessions in their sandboxes |
| `run-delivery.yaml` (`bootstrap.py`, a Gitea 1.22.6 of this machine), the paper's models for AHE-Beta's team on Fireworks: GLM-5.3 for Triage, the Architect and the PM (its OpenAI-compatible API), Claude Code 2.1.332 calling GLM-5.3 for one developer (its Anthropic-compatible endpoint); four U4 seeds (`o14`, `m03`, `o01`, `m01`), two rounds: `hermit train`, then `hermit eval --adapter final` | bootstrap's retry (the repository taken again by its marker, the clones by its record), round 1's sessions with AHE-Beta's users and their native records, the controller publishing their reports, Triage, the Architect's RFC, the divergent PM, admission, the OpenAgents worker's pull requests, review and merge, the sealed receipt, its import, round 2's binding and round 2's sessions on the delivered adapter | 3 Triage issues merged into an RFC, 3 PM requirements; 4 pull requests reviewed and merged; the delivered commit imported as delivered and bound; each round-2 session's native record names it. Ratings round 1: 4, 1, 2, 1; round 2: 1, 2, 1, 2 (no difference four sessions could show). About $9.2 of GLM-5.3 (the team; the actor and the user on a ChatGPT login), 1 h 53 min for the training step. On this branch as of the sixteenth review round; later review rounds have tightened setup and channel checks since (unit- and smoke-tested) |
| A mini cohort of four u8 seeds (`010-pivot-product-revenue`, `superset-goal-discovery`, `pylint-dev__astroid-2240`, `sqlfluff-subagent-review`, their u8 personas) and two rounds, a 2,400 s task budget: `hermit train` (round 1, the template's seed adapter, then one Codex Evolver step), `hermit eval --adapter final` (round 2) | rollouts with AHE-Beta's users, the learner reading only the users' verdicts and transcripts (no persona: checked in the material; the cohort had no session without interaction, whose verdict `utils/ahe_user.py` maps apart, unit-tested), an adapter change, the change's rollouts | round 1 ratings 4, 3, 3, 3 (mean 3.25); round 2 ratings 2, 3, 3, 4 (mean 3.0): no difference that four sessions could show. The learner's change: `ADAPTER.md` asks for an initial action, evidence-backed progress updates, prompt answers to status requests and identifiable reviewer findings, from the users' complaints (waits without visible progress, having to prompt for progress, merged reviewer results); it names no task. 2.45 M actor tokens in round 1, 3.04 M in round 2, 0.39 M for the learner |

Not run: the paper's cohorts (U8, U16, U32 over six rounds), its held-out evaluation on Harbor-index, more than one
round of AHE-Beta's own delivery Evolver, and any task with a companion container.
