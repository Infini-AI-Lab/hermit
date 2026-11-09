# Acting and learning on a Claude subscription

Nine small sentiment tasks, the same as [First real-model training](../../docs/get-started/first-model.md), run on your
own Claude subscription through the official Claude Code CLI, with no model API key. The acting agent is not told the
answer's form, so an adapter has something to learn: the judge wants a JSON object with one key, `label`. This is an
integration smoke test, and it promises no gain.

| Config | Acting | Learning | Route it shows |
|---|---|---|---|
| `run.yaml` | Claude Code in a workspace (`agent.base: claude`) | ACE, chat calls through `llm: {cli: claude}` | The workspace agent and a text-reply Evolver |
| `run_react.yaml` | `react`, whose model is the endpoint (`agent.model: {cli: claude}`) | ACE, the same | A pipeline that takes a model instead of a workspace |
| `run_workspace_learner.yaml` | Claude Code in a workspace | The `claude` Evolver edits the adapter | The learner's Base Agent path: tools, files, sessions |

The [guide](../../docs/guides/claude-subscription.md) has the sign-in, the boundaries, the plan, what a failed session
becomes, the model contract and which methods go which way.

```bash
python -m pip install -e .
claude setup-token                    # once: prints a token good for a year
read -r -s CLAUDE_CODE_OAUTH_TOKEN    # paste it
export CLAUDE_CODE_OAUTH_TOKEN
hermit check --config examples/claude_subscription/run.yaml --no-probe
hermit eval  --config examples/claude_subscription/run.yaml --adapter none
hermit train --config examples/claude_subscription/run.yaml
hermit eval  --config examples/claude_subscription/run.yaml --adapter final
```

The configs ask for `claude-haiku-5-5` at low effort, to spend little of your plan. On the command line,
`agent.model.name=claude-sonnet-5-5 llm.model=claude-sonnet-5-5` changes it for `run.yaml`,
`agent.model.model=... llm.model=...` for `run_react.yaml` (its acting model is a `{cli: claude, ...}` spec), and
`agent.model.name=... evolver.model.name=...` for `run_workspace_learner.yaml` (it has no `llm`). A session's overhead
counts: each working session starts with Claude Code's own instructions. `~/.cache/hermit/claude-login/plan/plan.json`
shows your plan's windows after the first session.

Every session runs in a bubblewrap sandbox: the acting agent in the task sandbox (`run_react.yaml`'s `react` there too,
its model's sessions each in a sandbox of their own outside it), the learner's chat calls each in a sandbox of their
own, and `run_workspace_learner.yaml`'s learner in the evolver sandbox. The sandboxes need Linux with bubblewrap.
Without it, `sandbox.enabled=false` (or `sandbox.enforce=best_effort`, which records it) runs the configs unconfined,
which is not for reported results.

## Real subscription smoke

2026-11-05, with the token of `claude setup-token` on a Claude subscription: Claude Code 2.1.318 on Linux (RHEL 9) with
bubblewrap, each config as it was then (`claude-haiku-5-5` at low effort, the sandboxes on) but with
`agent.timeout_min: 15`, on an early version of this route whose sessions still had the tools now denied
([the guide](../../docs/guides/claude-subscription.md#execution-boundaries)), before the one sandbox design (#78) and
under its keys at the time (the acting session in the rollout role's sandbox, `network: isolated`); `check`, then
`train` and `eval --adapter final` on each (and `eval --adapter none` on `run.yaml`). One pass over the three test
tasks each:

| Config | Training | Test (3 tasks) |
|---|---|---|
| `run.yaml` | 6 acting sessions (3 rollouts and ACE's 3 re-runs of the acting agent), ACE's Reflector and Curator in text sessions: 8 bullets, among them the one-key JSON form; 54,467 tokens in the rollouts, 58,448 in the re-runs and 15,620 in the learner's text sessions, 59 s | none 0/3, final 0/3: the values right, the form not (`**positive**` and the bullets it used) |
| `run_react.yaml` | `react` through the endpoint (3 rollouts, 3 re-runs), ACE as above: 7 bullets; 2,620 tokens in the rollouts, 5,595 in the re-runs and 15,812 in the learner's text sessions, 49 s | final 0/3, as above |
| `run_workspace_learner.yaml` | the `claude` Evolver, one Claude Code session in the adapter, which rewrote `ADAPTER.md` as a sentiment procedure with strict JSON-only output rules; 53,209 and 174,197 tokens, 27 s | final 3/3 |

What the 40 Claude Code sessions (47 turns) said of themselves: every one `apiKeySource: none` (the subscription paid),
every one served by `claude-haiku-5-5`, none failed, none on usage credits (the account has them off). The plan's 7-day
window went from 52% to 53% (`~/.cache/hermit/claude-login/plan/plan.json`); no limit was met, so a real refusal and its
hold were not seen. No run record holds the token: the runs, the plan's directory and the session homes were searched
for it and for its prefix. A text session's own overhead, measured apart through the same endpoint (its stream was not
kept): 361 input tokens for a two-character prompt with no system text.

Again on 2026-11-07, on this code after the one sandbox design (#78), each config as it is now (the acting
agent in the task sandbox, the learner's text sessions in a bubblewrap sandbox of their own, `react`'s model
sessions in one of theirs, the `claude` Evolver in the evolver sandbox; each sandbox's record lists
`api.anthropic.com` alone and the token by name), the same commands: the same scores (`run.yaml` none 0/3, final
0/3; `run_react.yaml` final 0/3; `run_workspace_learner.yaml` final 3/3). Its 38 sessions: every one
`apiKeySource: none` and served by `claude-haiku-5-5`, none failed, none on usage credits; no run record holds the
token. One more session in the task sandbox ran four commands from its Bash tool: the plan's directory was an
empty one of its own, its environment had no variable named with `OAUTH` (`env | grep -c OAUTH`: 0), and `curl
https://example.com` was refused by the sandbox's proxy.
