# AHE-Beta: harness evolution from simulated users

**Agentic Harness Engineering Beta: Requirement-Centered Evolution of Agent Harnesses**
([arXiv:2611.24924](https://arxiv.org/abs/2611.24924), [code](https://github.com/Curry09/AHE-Beta)) evolves a coding
agent's harness from what simulated users ask for while they work with it: ❶ a user simulator follows a persona
through multi-turn sessions (follow-ups, mid-run messages, interruptions, its own checks) and files issues and a 1–4
rating; ❷ a team (Triage, Architect, PM, Developers) turns the issues into requirements and the requirements into
general changes; ❸ a change merges only when it resolves a verified requirement and passes review. No verifier result
or benchmark score is used during evolution.

This example brings AHE-Beta's **user-driven evaluation** into Hermit. The learned object is a Codex adapter
(`template/`: instructions, skills, scripts, roles, hooks); Codex, the user simulator, the tasks and their verifiers
stay fixed. AHE-Beta itself evolves its own NexAU harness; this example does not run that harness.

| Route | Acting | User | Learning | State |
|---|---|---|---|---|
| `run.yaml` from [`prepare.py`](prepare.py) | Codex in each task's Harbor environment ([`utils/user.py`](utils/user.py)) | AHE-Beta's own simulator ([`utils/ahe_user.py`](utils/ahe_user.py)), its model through Hermit | Hermit's Codex Evolver on the users' verdicts | run (Validation, below) |
| `run-delivery.yaml` (`prepare.py --delivery-repo`) | the same | the same | AHE-Beta's own demand and delivery pipeline ([`delivery/`](delivery/): Triage, Architect, PM, admission; OpenAgents developers on a Gitea repository) | unvalidated beyond scripted runs |
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
| `agent` | the running Codex session ([`utils/channel.py`](utils/channel.py)): `write(text)` (the first message starts Codex with it as the prompt; a later one reaches the model after the running tool call, or as the next turn), `poll(timeout)` (the next reply `{text, interrupted}`, or None while it works), `send(text, timeout)`, `events()` (tool_start, tool, denied, delivered, adapter_block, timeout), `interrupt(text, timeout)` (tool calls are denied until it stops; a running tool is not cancelled), `close()` |
| `workspace` | the task's environment as the user reaches it ([`utils/user.py`](utils/user.py)): `run(command, timeout, cwd)` → `{code, stdout, stderr, timed_out}` as the task's agent user, `read`, `write`, `quiesce()` (the agent stopped and what it started ended, so a review sees frozen files); a task service's `service_run` / `service_read` in the main container |
| `params` | `user.params`, the task's assignment (its persona, scenario, round: `user.assignments` or the row's `user.task_field`), `session_id`, `artifact_root` (a private folder of this machine), `adapter_sha256` |
| returns | `{"score": a number in [0, 1], "rating", "outcome", "reason", "issues", "feedback", "usage", "files", "record", ...}` |

## How a session runs

One `codex exec` runs for the whole conversation, in the task's own Harbor environment, through Hermit's Codex agent
(`UserCodexAgent` subclasses `hermit.agent.acting.workspace.CliAgent`: the adapter's deployment, the sign-in, the
stream, the usage and the infra classification are the framework's). Three hooks ([`utils/user_hook.py`](utils/user_hook.py))
carry the conversation through a bridge ([`utils/channel_server.py`](utils/channel_server.py)), both installed as root
outside the workspace before Codex starts: PostToolUse inserts a waiting message, PreToolUse denies tool calls after an
interrupt, Stop hands the reply to the user and continues the session with the user's next message. The adapter's own
hooks stay; its Stop hooks run inside the user's Stop hook first (Codex runs one event's hooks at the same time), so
they must be command hooks without a matcher (the template's check refuses others). This machine talks to the bridge
with one command at a time in the environment (a read waits there up to 5 s for new events), so nothing is mounted from
this machine and the agent's writable workspace holds no queue. The session ends with the user's end, then
[`utils/processes.py`](utils/processes.py) ends whatever the rollout started (the bridge and the task's registered
services stay).

The bridge, its hook and `.codex/` are root's and read-only: a boundary only against an agent that does not run as root.
AHE-Beta's exported images run the agent as root (no `[agent] user` in their task.toml), so there the channel is out
of an honest agent's way but not out of reach; give a task an `[agent] user` to make it a boundary. The score never
depends on it: the judge reads the verdict this machine kept, not a file in the environment.

What a run keeps, and what a learner reads:

| Where | What | A learner reads it |
|---|---|---|
| `log/user-transcript.jsonl` | every message, reply and tool event the user could see, with times | yes (the trajectory) |
| `log/user-verdict.json` | score, rating, outcome, reason, issues, feedback | yes |
| judge feedback ([`utils/judge.py`](utils/judge.py): `user`) | the rating, outcome, reason and issues as text | yes |
| `<workspaces>/user/<session>/` | everything else the simulator left: its session record, the persona, its private checks | no (never in the log) |
| `assignments.json` | each row's persona, scenario and round (`prepare.py`) | no: the rows hold only id, Harbor task and instruction |
| the task's verifier | AHE-Beta's exported tasks' verifier only copies the user's score; a benchmark's tests are for evaluation | no score or feedback of it (judge `user`) |

A session whose user failed (the simulator raised or gave no valid score) is not the agent's result: `extra.infra =
user_failed`, which the run runs again in a fresh environment (`environment.harbor.infra_retries`) and, failing again,
records unscored, never as a 0. So is a Codex session that was infra. An adapter that failed before Codex started is the
agent's result (0).

## Setup

```bash
python -m pip install -e '.[test]' -r examples/ahe_beta/requirements.txt   # Hermit's environment: the simulator runs here
uv venv --python 3.13 ~/.cache/hermit/harbor-venv && uv pip install --python ~/.cache/hermit/harbor-venv/bin/python harbor==0.25.0
git clone https://github.com/Curry09/AHE-Beta "$AHE_ROOT" && git -C "$AHE_ROOT" checkout 43b165fff7b01e01f4fe4697b7597ccd61700727
codex login                                     # the ChatGPT login the configs use (or give <backend>/<model> references)
export HERMIT_CODEX_RELEASE=~/.codex/packages/standalone/releases/0.170.0-x86_64-unknown-linux-musl
```

The Codex CLI in each task's container is this machine's static release, mounted read-only at `/opt/hermit-codex`
(`environment.harbor.environment.mounts`) and named by `agent.codex.bin`: Hermit asks only its built-in agents what to
install in a task's environment (hermit.isolate), and `UserCodexAgent` is this example's class. Its version must match
`agent.codex.version` (`prepare.py --codex-version`). Each task needs `python3` in its image for the bridge.

## Train and evaluate

Export a cohort with AHE-Beta's exporter (its tasks as Harbor task directories, each round and slot one row, the
original persona schedule), then write Hermit's configuration beside it:

```bash
PYTHONPATH="$AHE_ROOT/backend" python -m ahe_beta.integrations.hermit.export \
  --experiment "$AHE_ROOT/examples/experiments/u8-mixed-e2b.json" --output <cohort> --cache <material cache>
python examples/ahe_beta/prepare.py --cohort <cohort> --ahe-root "$AHE_ROOT" --run-id u8-codex
cd <cohort>
hermit check --config run.yaml
hermit train --config run.yaml
hermit eval --config run.yaml --adapter final      # the last round, same cohort (AHE-Beta's R6), user-rated
```

One training step is one round (`step_size`: a round's sessions, `shuffle: false`, one epoch); the final split is the
cohort's last round, a same-cohort observation, not a held-out benchmark. The models default to GPT-5.6-Luna on the
ChatGPT login for the actor and the user and GPT-5.6-Luna for the learner (`--actor-model`, `--user-model`,
`--learner-model`: a Codex model name, or a `<backend>/<model>` reference such as
`fireworks/accounts/fireworks/models/glm-5p3-flash`, the paper's model family). The user's model on a login is a chat
endpoint over Codex (hermit.agent.base.endpoint), each request a Codex session in a bubblewrap sandbox of its own that
reaches only the login's hosts (hermit.isolate.cli_sandbox); its usage is the user's (`extra.user.usage`), not the
run's tokens, which are the actor's (the paper's "Tokens" exclude the user too).

### AHE-Beta's own Evolver

[`bootstrap.py`](bootstrap.py) provisions the Gitea repository from `template/`, a delivery clone and an OpenAgents
worker clone, and writes `delivery.json` and `run-delivery.yaml` (`prepare.py --delivery-repo`); start the worker with
[`openagents_launcher.py`](openagents_launcher.py) from the OpenAgents commit AHE-Beta's compatibility manifest pins
([`openagents_runtime.py`](openagents_runtime.py)), and the demand roles' models in AHE-Beta's own variables
([`experiment.env.example`](experiment.env.example)). The Evolver ([`delivery/evolver.py`](delivery/evolver.py)) runs
one full feedback and delivery cycle per step in AHE-Beta's own Python, copies back only the files the template's
contract covers, and checks the result with `check_spec`; a step that cannot deliver fails (the adapter stays).

## Benchmarks

| | Paper | Here |
|---|---|---|
| Evolution | 32 seeds (Appendix B): SWE-bench 9, Terminal-Bench 8, mechanism tasks 8, NL2Repo 4, TUA-Bench 3, each with a persona (15 cards of a 50-card library), 25 with interaction and 7 without; U8, U16 and U32 take the first 8, 16, 32; six rounds | the exporter's cohorts (`u8-mixed`, `u8-direct`, `u16-mixed`, `u32-mixed`), as Harbor task directories with AHE-Beta's images (`ghcr.io/mmjzmy/ahe-beta-tasks`) and inputs (its release `evolution32-task-materials-v1`) |
| Held-out evaluation | 38 Harbor-index tasks ([harbor-index](https://github.com/harbor-framework/harbor-index) `35f01ec`, release 1.4, AHE-Beta's `published38`): Coding 14, Optimization 12, Analysis 7, Tool Use 5; pass@1 by the tasks' own verifiers, user rating by one fixed evaluation persona that sees replies and a read-only diff, tokens per task | not packaged yet: in Hermit it is a Harbor task set (`environment.harbor`) judged by `utils/judge.py:verified` (pass@1, the user's rating beside it); an evaluation user that only reviews completed replies is a `simulate` of its own |

Tasks with a companion container (AHE-Beta's `kv-live-surgery`, `payments-pipeline-fix`, `080-convert-novel-epub`)
need `service_run` there, which a Hermit environment does not reach yet: their sessions fail as `user_failed`.

## The authors' implementation against this one

| Dimension | AHE-Beta | Here | Why |
|---|---|---|---|
| The harness learned | NexAU's whole harness (prompts, tools, middleware) | a Codex adapter: instructions, skills, scripts, roles, hooks; Codex fixed | the framework: only the adapter changes |
| User simulation | its persona loop, goal ledger, private checks, 90 s decisions, 24 turns, terminal review | the same code (its checkout's `ahe_beta.integrations.hermit.session`), its transport Hermit's channel | |
| What the learner reads | issues, sessions' interaction records and harness logs (Triage), ratings and summaries (PM) | the users' verdicts (rating, outcome, reason, issues) and transcripts, the agent's stream; no persona, no verifier | the boundary of the learning material (above) |
| Modification and selection | four roles on Gitea, admission, RFC, review, merge | `run.yaml`: one Codex learner, the protocol in `task-info/README.md`, `check_spec`; `run-delivery.yaml`: AHE-Beta's own | the validation route uses Hermit's Codex Evolver |
| Models | GLM-5.3-Flash acting and user, GLM-5.3 team, Claude Code developers | any: GPT-5.6-Luna on a ChatGPT login by default; GLM 5.3 Flash by reference | |
| Runtime | E2B sandboxes, AHE-Beta's controller | each task's Harbor environment (local Docker by default), Hermit's Trainer | |
| Not ported | the held-out evaluation suite, its evaluation persona, the human A/B study | | |

## Validation

See the PR for the run records.
