# Troubleshooting

Start with the failing command, config path, run name, and first error. Save the run's records before restarting.
`check --no-probe` inspects configuration and component loading; the ordinary `check` also runs one task and
can make paid calls.

| Symptom | Inspect first | Action |
|---|---|---|
| `hermit` not found | Active Python environment | Activate it and run `python -m pip install -e .`; verify `hermit --help` |
| Unknown key or wrong file path | Config spelling and config directory | Compare the [field reference](../reference/config-fields.md); resolve paths relative to the YAML |
| Template not found | `template` and its `.hermit/adapter.yaml` | Use a registered id such as `prompt-only@1.0` or a directory containing `.hermit/adapter.yaml` |
| GEPA/SkillOpt/Harness² cannot import | Installed method group and environment | From the checkout root, run `python -m pip install -e . --group gepa` (or `skillopt` / `harness2`) in the environment running the CLI; requires pip 25.1+ |
| `no such option: --group` | pip version | Run `python -m pip install --upgrade "pip>=25.1"`, then retry the group install |
| Learner authentication fails | Learner endpoint, key variable, backend CLI | Verify the configured variable is set without printing it; authenticate the selected CLI |
| `login: claude signs in with the token ...`, `Not logged in`, or a run that stopped with `hermit_login_failed`, on a Claude Code session (`agent claude: ...`, `evolver claude: ...`) | `CLAUDE_CODE_OAUTH_TOKEN` | Run `claude setup-token` and export the token, then rerun `hermit train`: it resumes after the last finished step. See [Claude subscription](claude-subscription.md) |
| `Not logged in`, or a run that stopped with `hermit_login_failed`, on a Codex session (`agent codex: ...`, `evolver codex: ...`: a 401, an invalid key, a refused proxy token) | The model reference's key variable; on `login: chatgpt`, the ChatGPT sign-in | Export a valid key, or on `login: chatgpt` sign in again as at first (`codex login`, into Codex's file store: [Run with your coding plan](../tutorials/codex-plan.md)), which replaces Hermit's older shared copy; then rerun `hermit train`: it resumes after the last finished step. See [A coding-agent CLI as the acting agent](../framework/task.md#a-coding-agent-cli-as-the-acting-agent) |
| On a Harbor task set, a rollout `infra` with the cause `login_failed`, and every rollout after it `infra`/`halted` | `<jobs_dir>/HALT` (the error and the trial's job) | Restore the sign-in or the key as above and remove `<jobs_dir>/HALT`, which nothing else removes: while it exists every rollout returns at once as `infra`/`halted`, a resumed run's too. See [Benchmarks](../framework/task.md#benchmarks) |
| Sessions on a login wait and nothing runs | The plan's files and report under `~/.cache/hermit/claude-login/plan/` (Codex: `codex-login/plan/`) | `plan.json` says which window is over and until when; remove `PAUSE`, raise `MAX_USED`, or wait for the reset. See [The plan](claude-subscription.md#the-plan) |
| `train refuses to start: ... serves [...], not 'responses'` (or `'messages'`) | The model's backend and the caller's dialect | Codex speaks the Responses API only, Claude Code the Messages API only: bind a backend that serves it (vLLM, SGLang, Fireworks, DeepSeek) or a translating proxy; `hermit backends check <ref>` tries each dialect. See [Model backends](model-backends.md) |
| Actor works but learner fails, or conversely | Separate actor/learner configurations | The pipeline owns acting-model settings; `llm` and backend parameters own learner settings |
| bwrap missing or namespace denied | Linux runtime and namespace policy | Install bubblewrap and use an environment permitting its namespaces; see [Installation](../get-started/installation.md) |
| Probe fails on `adapter=None` | Pipeline's baseline branch | Make no-adapter execution valid; do not assume every call has an adapter directory |
| Invalid split or no test tasks | Task ids and split files | Ensure ids exist and splits are disjoint; `eval` requires a nonempty test split |
| Adapter remains unchanged | Learner status, feedback, learnable paths | Read the step report; a valid no-op, perfect batch, rejected candidate, or validation gate can explain it |
| Unexpected files disappear | Specification-check issues | Add intended paths to a new template's learnable rules before training |
| Score does not rise after an update | Timing and held-out results | The current step's score precedes its update; inspect the next rollout and separate evaluation |
| Budget seems too low or unknown | Usage reporting and model prices | Include external calls and candidate tests; unknown cost is not zero |
| Timeout at epoch end | SkillOpt slow update or long agent session | Inspect method work and align per-session and Trainer timeouts |
| Run already finished | `summary.json` | Choose a new run name; do not expect a completed run to resume |
| Wrong run evaluated | Run-name/output overrides | Use the same overrides on train and eval; inspect the returned commit |
| Plugin conversion refuses context, roles or hooks | Target capability and `omitted` report | Use a supported host or workspace deployment; use `--allow-lossy` only if omitting those exports is intended |
| Host cannot find adapter tools | Generated command, Python, source directory | Keep workspace deployment's source paths available; check the host's MCP logs and tool imports |
| Streaming result differs from an aggregate eval | Window order and checkpoint selection | Use the recipe's evaluator and benchmark metric |

## If training fails midway

Read `steps.jsonl` to find the last completed boundary, then inspect that step's report and the process error.
An Evolver failure can be recorded and training can continue with the previous adapter; a process crash may leave
the run unfinished. [Recovery](runs.md#resume-an-interrupted-run) explains which state is restored.

Do not infer rollback of an external database, service, or API charge from adapter rollback. Those belong to the
task integration. Preserve evidence before deliberately restarting a stream.

## If the run succeeds but performance is worse

Check that the pipeline loads the adapter it receives, the learner sees informative feedback, and the judge
implements your intended objective. Compare `none`, `root`, and `final` under the same acting settings.
Inspect raw task errors before changing the learning method. A specification check validates files rather than
task quality; [Evaluation](evaluation.md) covers a defensible comparison.

## Reporting an issue

Include the framework commit/version, OS and Python version, method, command, config with credentials removed,
first traceback, and relevant step status. A small task/pipeline/judge reproducer is more useful than an entire run.
Review trajectories and feedback for private task content before sharing them.
