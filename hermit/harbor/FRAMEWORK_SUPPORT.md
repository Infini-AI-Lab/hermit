# Harbor and the Hermit core

The Harbor integration changes nothing in `hermit/` outside this package but one thing: Pi's and dsh's host
deployments (`hermit/hosts/pi.py`, `hermit/hosts/dsh.py`) take `serve_tools`, as the other four hosts already do, so
that a trial in a task's container gets the adapter's tools from the runner, not from a server its workspace would
start on this machine. The three agent layers (`hermit/agent/`), the Trainer, `hermit eval`, `hermit check`, the
configuration and the sandbox are unchanged. This note records how Harbor's needs fit the existing interfaces, and
what could later become framework support. Nothing here is required to run a Harbor task set.

## How Harbor fits the existing interfaces

| Need | Where it lives |
|---|---|
| A trial's verdict, usage and final message, from the CLI's stream in the task's container | `HarborAgent.session` (`runner.py`) copies the stream and the CLI's last message out of the trial's outputs, each read once as a regular file and never through a link, then reads the copies with the CLI Base Agent's `outcome` and `parse`, as a local session is read. |
| Codex in a trial: a lost sign-in, and a provider that could not serve | `signin.codex_signin_lost` and `signin.codex_unavailable` read a Codex trial's stream. The Base Agent's Codex reading reports no lost sign-in, since a local session that meets one fails on its own. It also takes a reconnect Codex recovered from for an outage. |
| A lost sign-in, or an API account's usage limit, ends the run | The runner raises `LoginFailed` (`hermit.agent.base.interface`), which the rollout passes up: `hermit train` stops and resumes after its last finished step, and `hermit eval` stops. The answer agent's relay turns a Codex session's lost sign-in into the endpoint's 401 with `LOGIN_FAILED`, as the endpoint already does for Claude Code. |
| Subscription plans shared by trials (Codex's ChatGPT login, Claude's token) | A trial waits for the plan, takes one of its slots and records what the session says of it with the Base Agent's login functions (`held`, `wait`, `slot`, `hold`, `note_usage`), as a local session does. |
| A sign-in a trial refreshed, copied back | `signin.take_back` checks the file first: a regular file of at most 64 KiB holding a ChatGPT sign-in's tokens, refreshed at most a minute ahead of this machine's clock (an hour for a container on another machine). Then `login.take_back` takes it. |
| The answer agent's model when only a CLI reaches it | `tools.ModelEndpoint` serves the benchmark file's `model: {cli: ...}` itself. Before each request it waits for the login's plan (`login.held`, `login.wait`), then answers through `endpoint.cli_answer`, and it counts what failed calls used from their error replies. A run config's own `{cli}` model is refused at startup: the pipeline serves it and waits for the plan inside the request, where the runner cannot tell the wait from the model's own time. |
| Checks before the first trial | `HarborAgent.check_agent`, run as the runner is built, checks the sign-in a trial is handed, web search, a model the containers can reach, bubblewrap, mini-swe-agent's package, and no secret in `acting_agent.env` (Harbor's own rule for secret names). `hermit check` builds the runner too (`agent.name: harbor` runs on the host) and reports the first of these refusals. A run config's own `{cli}` model for the answer agent is refused both as `hermit check` hands it (as written) and as `hermit eval` and `hermit train` do (the endpoint the pipeline serves it from, `PIPELINE_CLI_SPEC`, which must stay the keys of `main`'s `endpoint_spec`). |
| The acting model in a trial | `launch.binding_of` and the model layer's `bind_agent_params` bind it for the CLI. A benchmark file names a model by name, or `{cli: ...}`; a `<backend>/<model>` reference belongs in the run config (`check_shared_model`). |
| The adapter in the task's container | `staging.deploy_here` calls the host's own `prepare(..., context=..., serve_tools=False)` in a child process under the strict adapter sandbox. The runner serves the adapter's tools over HTTP (`tools.AdapterTools`), and the Base Agent's `prepare_home` names them for the session. A deployment or per-task context that fails in that child is the agent's result (`adapter_failed`: `agent_error`, scored 0, its message the feedback); an OSError on this machine is infra (`staging_failed`). |
| Evolver methods reading Harbor trials | `curate.py` lays out each trial's log as a local session's: the CLI's stream (`agent-<cli>.jsonl`), the hooks' `hook-calls.jsonl` and `hook-state/`, `task.md`, `prompt.txt` and `last_message.txt` at its top. The trajectory readers (`read_steps`, `log_steps`), the AHE Debugger, RRSI and SkillOpt read it unchanged. |
| Continual Harness telling the agent's timeout from the verifier's | `HarborAgent.verifier_timeout_verdict` never marks a verifier that ran out of time as the agent's `timed_out`. |
| Infrastructure failures (Docker, the network, the provider, Harbor itself) | `HarborAgent._run_trial` runs such a trial again, `agent.params.infra_retries` times (2) after `infra_wait_s` (300 s), unless its cause runs no better again (`NO_RETRY`: a disk gate that waited its time out, a provider's refusal of the run's configuration, `model_rejected`). One that still fails is refused by the judge (`judge.Unscored`), so the rollout records a failed run (`status: judge_error`), metered and with its log, as EvoSafe's judge does. `hermit eval` counts it as 0 in `mean_score` and among its `errors`. `python -m hermit.harbor.report <eval dir>` gives pass@1 over the trials that are the agent's result, with the infrastructure failures apart. |
| Multi-step tasks (streams) | Each runner process names its own streams (`<jobs_dir>/streams/<task>__s<rollout>__<runner id>`) and stops the unfinished ones when the runner is freed or at exit. A stream's steps run in the split's order (`data.shuffle: false`); a step whose turn cannot come is `stream_order`, a failed run. A step that ends in any way before its driver is asked for it gives its stream up, so that the driver is stopped and the stream's next pass starts anew. A resumed `hermit train` (a new runner) loses a stream's remaining steps in that epoch (`stream_order`). |
| Evaluation-only configs | `Config.validate` asks every config for `data.train`, `template` and `evolver.name`; a config that only evaluates gives placeholders. |
| Harbor's own agents (`acting_agent: {name: ...}` or `{import_path: ...}`) take no adapter | Each rollout's `run()` refuses an adapter for one (a ValueError), before anything is staged. |
| A model a CLI reaches, for the answer agent | Its sessions run on this machine in the sandbox main gives such sessions (`hermit.isolate.cli_sandbox`, role `agent-model`), the user's own secrets proven absent (`HOME_SECRETS`). |

`hermit/harbor/` uses four private helpers of main: `hermit.adapter.session._adapter_sandbox` and
`hermit.agent.base.endpoint._DROP` (`tools.py`), `hermit.agent.acting.workspace._query` (`context.py`) and
`._session_args` (`launch.py`). `launch.check_settings` repeats `CliAgent.run`'s and `MiniSweAgent.run`'s checks of
their settings, so that a trial is refused before its container. A change of main to any of them breaks or silently changes the Harbor runner.

There is no trial cache: a resumed eval or training step runs its trials again.

## Candidates for framework support (open for discussion)

The Trainer and `hermit eval`:

1. **A run that is not a verdict.** A `RunOutput` could say it is not the agent's result (an infrastructure
   failure), so that the Trainer and `hermit eval` leave it out of the mean and can run it again, instead of a task
   routing it through its judge and a separate report.
2. **Evaluation-only configs**, which need no `data.train`, `template` or `evolver.name`.
3. **Rows that name their epoch, their repeat and who runs them.** If the Trainer handed the pipeline the epoch and
   repeat of each row, a pipeline could keep its trials, and a resumed eval or training step could reuse the finished
   ones; if a row said whether the Trainer's rollout or an Evolver's `run_agent` runs it, a candidate's run of a
   multi-step task's step could run in a stream of its own, never in the Trainer's (out of its turn there, or a pass
   of its first step that the Trainer's next epoch inherits).
4. **A run identity for the pipeline**, so that a resumed run continues its multi-step streams, instead of losing a
   stream's remaining steps in the epoch it resumes.
5. **A multi-step task's step order**, checked by the Trainer rather than kept by `data.shuffle: false`.
6. **Paths a config declares**, kept off Harbor's task and job directories (the overlap check in `hermit/isolate.py`).
7. **`hermit check` for Harbor task sets**: a trial's sign-in, the acting model's binding for its CLI, and an adapter
   refused for Harbor's own agents; and `hermit check`'s and `hermit train`'s refusal of a `login: claude` run with no
   `claude` on PATH and no `claude_bin`, though a trial runs the container's CLI.
8. A refused `hermit eval` that keeps the earlier eval's files; `hermit check`'s check of a test row's own `repeat`;
   one-line config errors.

The agent layers:

9. **Reading CLI streams**: lines split on `\n` alone (a message's U+2028), very deep JSON, Codex's quotes of tool
   calls in its log, OpenCode's HTTP status.
10. **Codex's lost sign-in and outage**, read in the Base Agent as `signin.py` reads a trial's, so that Hermit's own
    Codex sessions end the run on a lost sign-in and take a recovered reconnect for no outage.
11. **Stricter checks on a sign-in copied back** from Hermit's own sandboxed sessions.
12. **The pipeline's CLI endpoint saying that a request waited for its login's plan**, so that a run config's own
    `{cli}` model could serve the answer agent.
13. **Hosts**: Pi's tool server in the strict adapter sandbox, as the others' are; `hermit/agent/acting/workspace.py`
    passing `serve_tools` to Pi and dsh for its container sessions.
14. **`Binding.spec` keeping `developer_role`.**
15. **Clocks of AHE and its Debugger** that leave out a wait for the login's plan.
16. **Harness²** for more CLIs; MWM's file names and its round count; RRSI Adapter's `halt_file`, which still names
    Harbor's `<jobs_dir>/HALT`, a file the runner no longer writes.
17. **Usage of a failed session** counted for learners' `llm: {cli: ...}` calls.

The rollout, the sandbox and `main`'s private helpers:

18. **A pipeline's usage when `LoginFailed` ends the run**: a trial a lost sign-in ended records nothing, the usage of
    its earlier runs included.
19. **The run's own secret paths** (`run_secrets`) for the sandbox of the answer agent's CLI sessions, which needs the
    config the runner never gets.
20. **Public names** for the four private helpers above.
