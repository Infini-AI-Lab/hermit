# Acting and learning on a Claude subscription

Run an acting agent and a learner on your own Claude subscription (Pro, Max, Ultra, Team or Enterprise) through Claude
Code, with no model API key. The same Base Agent does both: `agent.base: claude` and `evolver.base: claude` work in a
workspace, and `llm: {cli: claude, ...}` makes it the model of code that only calls a chat API.

Who may do this is Anthropic's to say, and it decides how this route is built. Its
[terms](https://code.claude.com/docs/en/legal-and-compliance#authentication-and-credential-use) let "an end user" sign
in "to the unmodified Claude Code binary with their own Claude subscription", and say that developers "may not collect,
store, or intermediate Claude.ai credentials or session tokens" and may not "route requests through Free, Pro, Max, or
Ultra plan credentials on behalf of their users". So Hermit runs the official CLI as installed, for you, on your
machine. It holds no credential: your token passes through the environment to the CLI (into a sandbox by name, for the
call that runs the session; into a container, to Docker by name and from there to the CLI on a file descriptor), and
Hermit copies, stores and refreshes nothing. Do not build a service for other people on this route: use an API key
([Model backends](model-backends.md)). The advertised plan limits "assume ordinary, individual usage", and a training
run is many sessions: start small and watch your usage ([The plan](#the-plan)). This page is no legal advice; the terms
decide.

## Sign in

Once, in your own terminal:

```bash
claude setup-token                    # opens the browser, prints a token good for a year; it saves nothing
read -r -s CLAUDE_CODE_OAUTH_TOKEN    # paste it: stays out of your shell's history
export CLAUDE_CODE_OAUTH_TOKEN
```

This is [Anthropic's way for scripts](https://code.claude.com/docs/en/authentication#generate-a-long-lived-token): the
token can only make model requests and has no refresh. `hermit check` and `hermit train` refuse a run on the login
when the variable is not set or the CLI is missing or too old (2.1.248 or later; these pages were tested on 2.1.318).

Why not the sign-in you already have (`claude auth login`, in `~/.claude/.credentials.json` or the macOS Keychain)? A
session needs a home of its own, and one working from a copy of that file refreshes it when its access token nears
expiry: the refresh token rotates, and your own Claude Code is left holding a dead one. A run could sign you out of your
own sessions. Codex's route (`login: chatgpt`) keeps a shared copy of its login for this reason; here the token makes
the copy unnecessary, and the terms above advise against one.

Nothing else may pay for a session on the login. In print mode the CLI takes an `ANTHROPIC_API_KEY` over any sign-in
without asking, and a settings file can bring in another key, a key helper or another host. So the session's environment
is rid of those variables, its last argument is a `--settings` that blanks them in every user, project, local and
session settings file (an adapter's hooks could write one mid-session), and a session that still reports another payer
(`apiKeySource` in its first event) is no answer: the run stops. Managed policy settings, which an organization puts on
a machine, outrank `--settings`: a managed key or key helper is caught as another payer, but a managed host or provider
switch is not, so on such a machine check that its policy sets none.

## Configure

The whole of
[`examples/claude_subscription/`](https://github.com/Infini-AI-Lab/hermit/tree/main/examples/claude_subscription) runs
on nine included tasks. Each setting is per layer, as for Codex ([Agent layers](../framework/agent-layers.md)).

An acting agent in a workspace and a text-reply learner (`run.yaml`):

```yaml
agent:
  base: claude                                                 # Layer 2: Claude Code in a workspace
  model: {name: claude-haiku-5-5, login: claude, effort: low}  # Layer 1: a model name on the login (required)
  claude: {web_search: false, timeout_s: 600}
llm:                                                           # the learner's model: one session per chat call
  cli: claude
  model: claude-haiku-5-5
  login: claude
  effort: low
  isolation: sandbox                                           # the default: a bubblewrap sandbox of its own
  timeout_s: 600                                               # one session's clock
  timeout: 700                                                 # the client's, past it
evolver: {name: ace}
environment: {network: no-network}                             # the default: the acting session reaches its model only
```

The acting agent runs in the task sandbox (bubblewrap), each of the learner's chat calls in a sandbox of its own. An
acting pipeline that takes a model instead of a workspace (`run_react.yaml`): `agent: {base: react, model: {cli:
claude, ...}}`. The endpoint runs outside the task sandbox, each request one session in a sandbox of its own, and the
task sandbox sees only a local URL and an ordinary model spec, so the token stays out of it. A workspace learner, the
separate Base Agent path (`run_workspace_learner.yaml`): `evolver: {name: claude, base: claude, model: {name: ...,
login: claude}, claude: {web_search: false}}`, in the evolver sandbox. OfficeQA's configs run every acting session in a
container of its own instead (`environment.image`), as Codex's run there ([Execution
boundaries](#execution-boundaries)).

Then:

```bash
hermit check --config examples/claude_subscription/run.yaml --no-probe
hermit train --config examples/claude_subscription/run.yaml
hermit eval  --config examples/claude_subscription/run.yaml --adapter final
```

## Execution boundaries

| Session | What holds it | Network | Sees |
|---|---|---|---|
| Acting, in a workspace, `environment.image` | a Docker container of its own, started from a worker on the host, as Codex's; Claude Code's own sandbox holds the session's commands inside it | the container's egress proxy: `api.anthropic.com` only; the commands: none | its workspace, a home of its own, the task's data read-only at its target; the commands see no process but their own and write only the workspace and `/tmp` |
| Acting, in a workspace, in the task sandbox | the task sandbox (bubblewrap), built from `environment` | `environment.network`: `no-network` by default, where `api.anthropic.com` (the login's host) passes its allowlist proxy by itself; `allowlist` adds `environment.allowed_hosts`; `public` is the host's network (warned) | what `environment.read` shows, its workspace (a task's data mounted in it is bound there, read-only, a real directory as in a container: a search that walks the tree, `grep -r`, `find` or Claude Code's Glob and Grep, does not follow a link), a home of its own |
| Learning, in a workspace | the evolver sandbox | its models' hosts (`api.anthropic.com` on the login), the Trainer's tool endpoint and `sandbox.evolver.allowed_hosts` | `/w` (the adapter, the learning material, the task description, ...), a home of its own |
| A text reply (`cli: claude`) | no tools at all: `--restricted`, `--tools ""`, `--strict-mcp-config`, `--disable-slash-commands`, no settings or memory files, `@path` left as text, an empty directory; a session reporting tools or MCP servers has failed. `isolation: sandbox` (the default) runs it in a bubblewrap sandbox of its own (`sandbox/llm-<n>.json`, an acting pipeline's `agent-model-<n>.json`: its empty workspace and home, nothing of the run, no variable of the shell but the token, by name), as Codex's text session runs in its; `isolation: container` in a container of its own | `api.anthropic.com` only | nothing |

A text session's `isolation` is `sandbox` by default, as Codex's, and needs Linux with bubblewrap; `container` needs
Docker and Claude Code's image. `none` is refused: with the run's sandbox off (`sandbox.enabled: false`, macOS) it runs
unconfined as everything else does, and `sandbox.enforce: best_effort` runs it unconfined where bubblewrap is missing.
A Codex text session can still run commands inside its sandbox; a Claude Code one has no tools at all. `cli_problems` in
`hermit/agent/base/endpoint.py` has the rule for each.

A container for Claude Code is Codex's (`hermit/agent/base/container.py`,
[the limits](../framework/agent-layers.md#limits)). `docker/claude/` builds the image from the machine's own Claude
Code, one file of the version the image is named for (it does not update itself there), root's and execute-only for the
session's user: the kernel then keeps the running CLI's memory and descriptors from every other process of that user.
The internal network is the one Codex's containers use, with an egress proxy of its own that lets `api.anthropic.com`
through and nothing else. For an acting agent `environment.image` names the image and `environment.container:
{network, proxy}` the rest (else `HERMIT_CLAUDE_NETWORK`, `HERMIT_CLAUDE_PROXY`); a text session's spec takes
`container: {image, network, proxy}` (else `HERMIT_CLAUDE_IMAGE`, `HERMIT_CLAUDE_NETWORK`, `HERMIT_CLAUDE_PROXY`).
Before its first session the agent checks the image, the network, the image's Claude Code (2.1.318 or later) and that
its sandbox starts there; the machine's own `claude` is not run. As for Codex, the worker that starts the containers
runs on the host and puts the adapter's code its deployment renders or serves (its context, its tools) in the strict
sandbox. Inside, the last `--settings` turns on Claude Code's own sandbox, as Codex's workspace-write
sandbox runs in its container: the session's commands (its Bash tool) run in bubblewrap with a process namespace and a
`/proc` of their own, have no network (no host is allowed and none is asked for), and write only the workspace and
`/tmp`; a command the sandbox cannot hold is not run, none may ask to run outside it, and no project or local settings
file can widen it. Claude Code's own file tools (Read, Write, Edit) run in its process, outside that sandbox: they reach
the workspace, the home, `/tmp` and the task's data, as Codex's process reaches them in its, but may not edit its
settings in the workspace (`Edit(.claude/**)` is denied, which holds under `bypassPermissions`), where a hook written
mid-session would run outside the sandbox. Claude Code tells the model of its sandbox in a section of the first message
(how it is set up, and that a task it cannot finish with what it was given should say what is missing, not reach past
it), and lengthens the Bash tool's description: 3.3 K tokens more in an OfficeQA session's first request (the median).
For the sandbox's `/proc` the container gets `--security-opt systempaths=unconfined` besides Codex's options (the
session runs as your user, not as root).

Every Claude Code session Hermit starts is denied, besides `WebSearch` and `WebFetch` unless `web_search: true`, the
tools whose work outlives the session or reaches past it: `RemoteTrigger` (a routine on your account), `CronCreate`,
`CronDelete`, `CronList` and `ScheduleWakeup` (prompts for later), `ListAgents` and `SendMessage` (your other Claude
sessions). A session is one task or one learner's turn: a learned skill or hook could otherwise leave work running on
the plan after the run. The rest stays, `Task`, `Workflow` and `Monitor` included: a session may start sub-agents, whose
model calls draw on the plan too. The login's plan directory ([The plan](#the-plan)), which every session on the machine
obeys, is not in a container at all: the session waits for the plan, takes its slot and reports to it from the process
that starts the container. Nor is it in the evolver sandbox or a text session's, whose sessions wait in the process that
starts them. In the task sandbox the agent's worker has it writable (a judge never), and a working session's commands do
not see it: a nested bubblewrap puts an empty directory of the session's own in its place, so a command can neither
change nor lock it (as on the host, when the pipeline runs there). Where bubblewrap cannot nest, a session in the
task sandbox is refused while `sandbox.enforce` is `required` (each task an error record saying so); with
`best_effort`, or on the host, Hermit warns once and runs it as it is.

The token in a workspace session. In a container, Docker gets it by name, and the container's shell hands it to Claude
Code on a file descriptor (a pipe Claude Code reads once) and takes it out of the environment before Claude Code starts:
no process in the container has it in its environment, the session's commands cannot see Claude Code's process, Claude
Code's Read tool refuses a process's environment under `/proc`, and the descriptor is empty once read. What runs beside
Claude Code outside its sandbox (a deployment's hook or MCP server) can neither read it from Claude Code's memory nor
write into the session's stream, since the CLI is undumpable, and the session cannot give itself such a hook. While a
container runs, `docker inspect` shows its environment, the token among it, to whoever may use Docker on the machine.
In a sandbox the token passes by name for the call that runs the session (the task sandbox's worker, the learner's or
the text session's command), never to the sandbox's other commands or its judge: an `environment.env` or
`sandbox.evolver.env` pattern that would take it in (`CLAUDE_CODE_*`, `*_TOKEN`) is refused when the config loads;
only its own name, written out, grants it to the whole sandbox, on purpose. Without a container the CLI keeps the
token from the environment of the commands it runs, but a command can read it from a parent process
(`/proc/<pid>/environ`): an adapter's hook or a learned script is such a command. During the session the sandbox's
network bounds such a command: in the task sandbox `environment.network`, `no-network` by default, lets it reach
`api.anthropic.com` and nothing else, and `public`, which `hermit check` warns of, any host; in the evolver sandbox it
reaches the learner's models, the tool endpoint and `sandbox.evolver.allowed_hosts`. Either way, right after each
session on the login Hermit replaces the token's exact value by its name wherever the session left it (its event stream,
its stderr log, the CLI's transcripts in a kept home), so the run's records, the learning material and the prompts a
learner builds from them do not carry it (a cut or encoded copy, or one written into a file, is not found); the run's
scrub of key values (`hermit/redact.py`) writes the same `<CLAUDE_CODE_OAUTH_TOKEN>` over what the run keeps. If a token
may have left, revoke it at claude.ai and make another.

## The plan

Sessions on the login share its plan as Codex's do on a ChatGPT login, in a directory of its own
(`~/.cache/hermit/claude-login/plan/`, `plan_dir` to name another), read again before each session:

| File | Effect |
|---|---|
| `PAUSE` | while it exists, no session starts |
| `MAX_USED` | a number: no session starts while a window that has not reset is at or above that percent |
| `SLOTS` | a number: at most that many sessions at once on this machine |
| `plan.json` | the latest report: each window's use and reset, from a session's `rate_limit_event`, and a hold in force |
| `plan.jsonl` | one line per report: its most-used window's use and reset (the window not named) |

A session the plan refused (the CLI's `api_error: usage_limit_reached`) is no answer. It holds every session until the
reset it names and runs again. `plan_max_wait_s` bounds the wait (default 12 h, `null` for none); past it the session is
`unavailable`. Limits per model family (an Opus limit) hold every session too. A hold lasts to its own end: sessions
that started before it and end after it do not lift it, and a later refusal that names an earlier reset does not
shorten it. The clocks around a hold still run: `trainer.call_timeout_min` stops a learner's step that waits past it, so
give a run that may meet a limit room there, and `agent.timeout_min` ends a task's worker, wait included, as an error
record that scores 0: leave it unset on a login. On a chat route (`llm` or `agent.model` with `cli: claude`) the session
waits inside the request, and the client waits with it: its `timeout` is extended by `plan_max_wait_s`, so a held call
is served after the hold instead of given up.

Usage credits are a bill apart from the plan, and the CLI has no switch against them: past the limit, an account with
credits on keeps being served. Hermit reads `isUsingOverage` from each session: the session that drew credits has
already run, and the ones after it wait for the plan's window, unless you set `usage_credits: true`, which also lets
your run's sessions past a credits hold another run wrote (`MAX_USED` still holds them). A refusal holds every
session, credits allowed or not: it replaces a credits hold's reason, never the other way round. To be sure of no
extra bill, turn credits off at claude.ai.

`plan_dir`, `plan_max_wait_s`, `usage_credits` and `plan_rerun` are the Base Agent's settings: for a workspace session
in `agent.claude` or `evolver.claude`, beside `web_search` (an acting agent takes the first three), and on a chat route
in the `{cli: claude, ...}` spec, beside `login`. They are not Layer 1's: under `agent.model` or `evolver.model`, which
take a model's name, login, effort and price, the config is refused when it loads.

The directory is shared with the sessions it steers and written by processes outside any sandbox (a learner's sessions,
other runs) and by the task sandbox's workers, so Hermit reads and writes its files only as regular files of its own: a
link, a pipe or a directory a session left at one of its names is replaced, never followed. What they say is checked: a
number that is not finite counts as none, a reset more than 31 days away as 31 days, `SLOTS` is held to 0 to 1,024, and
a writer gives up when the plan's lock stays held past two minutes (the report is not kept; a session that was to write
a hold waits five minutes itself).

## When a session fails

Claude Code ends a failed session with a result too, usually `subtype: success` with `is_error: true`, its text the
failure. It is read from the CLI's own fields, never from what the session's text says, and is never a reply:

| What happened | How it is read | A chat call | An acting task |
|---|---|---|---|
| The plan's limit | `api_error: usage_limit_reached` | waits and runs again; 429 with `plan_rerun: false` | waits and runs again |
| Throttled, overloaded, a server error, no connection (the CLI retried first) | the failed message's `error`, the status | 503 (the client retries) | runs again (`retries`), then an error record |
| The sign-in cannot serve (bad token, no access, a held account, no credit) | `authentication_failed` and its kin | 401 with the code `hermit_login_failed` | the run stops |
| The request itself: too long, no such model, the model refused, its output limit | `invalid_request`, `model_not_found`, `max_output_tokens`, `stop_reason: refusal` | 400, not retried | an error record |
| No result: killed, interrupted (the CLI can exit 0 here), a command taken for the prompt | no successful last result | 502 | an error record |
| Past `timeout_s` | the Base Agent's clock | 504, not retried (`x-should-retry: false`): it would run out its clock again | an attempt with no answer (`fail_reason`), as for Codex |

An acting task's error record is `status: error` with score 0.0 and no judge call. The workspace agent's keeps the
session's tokens and log; a pipeline whose model is the endpoint (`agent.model: {cli: claude}`) only sees an HTTP error,
so its record has neither, and the session's stream is under `sandbox/agent_sessions/`. A lost sign-in ends the run
unfinished and past any method's `except Exception`: restore it and `hermit train` resumes after the last finished step.
The same goes for a learner's session, a method role's included, and an agent Evolver's session that ended without a
result commits no half-finished edit. During `finalize` it ends the run unfinished only when every epoch had run (the
resume goes straight back to finalize); after an early stop or a budget stop, finalize fails and the last adapter is
final, as for any failed finalize, so a resume never trains past the stop.

Usage: input tokens count everything sent (fresh, read from the prompt cache, written to it). A subscription has no
per-token price, so `cost_usd` is unknown and the CLI's `total_cost_usd`, an estimate at list prices, is not kept: a
`budget.usd` bounds nothing here, so set `tokens` or `hours`. Each text session's event stream is under the run's
`llm_sessions/`, an acting endpoint's under `sandbox/agent_sessions/`.

Not yet counted or stopped: the tokens of an attempt that is run again (a plan's rerun, an acting retry, a chat client's
retry) and of a failed session through the acting endpoint are not added to the records or to `budget.tokens`, which
therefore undercounts; a rollout waiting for the plan is not stopped by the Trainer's budget or stop until its wait ends
(as on Codex's ChatGPT login). A lost sign-in met inside a Trainer tool that a learner's session calls over the tool
server (`run_agent`, `llm`) stops the learner and ends the run after its call, unfinished, as at a step.

## The model contract

A session is not a model API. This is what a chat request through the endpoint gets, on either CLI:

| In the request | What happens |
|---|---|
| `system`, `developer` messages | The session's instructions: on Claude Code they replace the CLI's own (`--system-prompt`), on Codex they follow its base prompt |
| `user`, `assistant` text | One prompt on stdin: one user message as it is, several turns written out as a transcript that ends with the turn to answer. A prompt starting with `/` is kept from being run as a CLI command |
| Parts that are not text; `tool` messages; `tool_calls` | 400 |
| `tools` (non-empty), a named `tool_choice`, `functions` | 400: a session cannot hand tool calls back. A role that needs tools runs a Base Agent session with them |
| `reasoning_effort` | The session's effort; on Claude Code low, medium, high, xhigh or max, else 400 |
| `model` | Must be the endpoint's; another is 400, never a quiet substitute |
| `max_tokens`, `max_completion_tokens` | Accepted and not applied, and the reply's `unapplied` says so: methods send them as a cap, and the CLI's own cap would cut the reply and continue it by itself |
| `temperature`, `top_p`, `stop`, `seed`, `logprobs`, penalties, anything else | 400: the CLI has no setting. `llm.unapplied: [temperature]` accepts a named field going unapplied. Pi applies `temperature` and `top_p` where its backend speaks an OpenAI-compatible API |
| `n` above 1; a `response_format` other than text | 400 |
| `stream`, `stream_options` | Kept: the reply arrives whole, as chunks at the end |
| `parallel_tool_calls`; `tools: []`; `tool_choice` `none` or `auto` | Accepted: they ask for nothing a session would do |

The reply is the session's whole message (`content`; `result` alone holds only its last block), `finish_reason` `stop`,
or `length` for a message that stopped at a limit, its usage, and the model the CLI says it used.

What differs from calling the model: Claude Code always puts its own identity line before your system text and an
environment message after your prompt (working directory, platform, date, model name), and no flag removes them. Its
default extended thinking stays on and counts toward usage, and thinking is not returned. Cache writes happen on every
session (each is a new conversation) and count toward your plan. A session that only replies costs about 360 input
tokens of overhead (claude-haiku-5-5, Claude Code 2.1.318: a two-character prompt with no system text came to 361).

A pipeline fits the endpoint if it needs text back and sends what the table takes. `react` fits with no tools to offer
(task or adapter). OfficeQA's direct chat, CL-Bench, ServeLearnBench and AgentDyn do not: their tools come back as tool
calls, and they are refused.

## Methods and roles

| Method | Role | Route | On Claude Code |
|---|---|---|---|
| GEPA | reflection | `ctx.agent` through `llm` | `llm: {cli: claude}`; no `temperature` unless `unapplied` |
| SkillOpt | analyst, merge, rank, slow update, meta skill | `ctx.agent` through `llm` | the same |
| ACE | Reflector, Curator | `ctx.agent` through `llm` | the same; `use_bulletpoint_analyzer` sends a temperature, so it needs `unapplied: [temperature]` |
| ReHarness | revision, reflection | `ctx.agent` through `llm` | the same; embeddings need their own endpoint or BM25 |
| Continual Harness | Refiner passes | `ctx.agent` through `llm` | the same |
| EvoSafeHarness | proposer; Designer, Criticizer, Analyzer | `llm`, or a session per role | either |
| Prompt-only, `claude` Evolver | learner | a Base Agent session in a workspace | `evolver.name: claude` |
| Meta-Harness Adapter | proposer | a Base Agent session in a workspace | `evolver.base: claude` |
| RRSI Adapter | proposer; analyst, digesters, critic | Base Agent sessions | `evolver.base: claude`: the roles follow it. The analyst and the critic have no tools, a digester `Read`, `Grep` and `Glob` |
| AHE Adapter | Evolve Agent, Agent Debugger | Base Agent sessions | `evolver.base: claude`: both follow it |
| MWM | learner | a Base Agent session in its view of the evolver sandbox (`/w`, `/evidence`, ...) | `evolver.params.backend: claude` |
| Harness² | editors, composer, reflection, selector | Base Agent sessions, Claude Code by default | `login: claude` with a model name; on a login a session prints its whole event stream, so the plan and the payer check see it as any other's. Its executions run on the run's acting agent: Claude Code takes the `harness2-codex` tree in its own places, and the editors read its stream in upstream's grammar |

What still depends on Codex: Harness²'s editors are told of upstream's Codex surface (`AGENTS.md`, `.agents/skills/`,
`.codex/hooks.json`), the tree Claude Code runs too, in its own places (`CLAUDE.md`, `.claude/skills/`,
`.claude/agents/`, `.claude/settings.json`); upstream has no Claude Code substrate. In the task sandbox (no image),
Codex still gets a task's mounts as links. The recorded results in these pages are on the models they name: OfficeQA's
third column (below) is the one on Claude.

Platforms: Linux with bubblewrap for everything above, and Docker for `environment.image` and `isolation: container`.
On macOS the token works without the Keychain, and `sandbox.enabled: false` runs everything unconfined, a text session
too; this has not been run on macOS. Windows has not been run. On a machine that reaches the internet only through an
egress proxy (`HTTPS_PROXY`), the sandboxes' allowlist proxies go out through it
([How the sandbox works](sandbox.md#network)).

## What was checked, and how

Scripted, in `tests/core/test_claude_subscription.py`: every shared entry point against a stand-in that replays streams
the real CLI printed (`tests/fixtures/claude_streams/`), the real binary on a scripted local model (no login, every
proxy closed; `--integration`), and the real image in its container on a scripted model at the network's gateway
(`--integration` with `HERMIT_CLAUDE_IMAGE` and `HERMIT_CLAUDE_NETWORK`): the token reaches the model by its descriptor,
and the session's commands find it in no environment, see no other process, reach no host and cannot write the home.
These show what Hermit does with what the CLI prints. They cannot show what the real service sends at a real limit.

A real subscription, recorded apart, ran the following, all before the one sandbox design (#78) and under its keys at
the time (a container was the acting agent's `isolation: container`, today `environment.image`; the bubblewrap sandbox
the rollout role's, today the task sandbox). The example's three configs: the workspace agent with ACE learning through
`llm`, `react` through the acting endpoint, and the `claude` Evolver in a workspace
([`examples/claude_subscription/README.md`](https://github.com/Infini-AI-Lab/hermit/blob/main/examples/claude_subscription/README.md#real-subscription-smoke)),
on an early version of this route: its sessions still had the tools now denied, and its configs set
`agent.timeout_min: 15`. OfficeQA's no-skill evaluation, 172 questions with Claude Haiku 5.5 acting, each session in a
container of its own (0.599; a pass before the review's last fixes, 0.640; after the rebase onto the one sandbox
design, 0.616), and one learner's call in a container
([`examples/officeqa/README.md`](https://github.com/Infini-AI-Lab/hermit/blob/main/examples/officeqa/README.md#results)).
Before them, on this route as it was before its review and without containers (the mounts bound and five of the seven
tools denied, not yet `ListAgents` and `SendMessage`; the plan not yet hidden from a session, no hook events in the
stream, no scrub of the token): two passes of the same evaluation (0.622 and 0.634), and Harness² acting and learning on
the first two questions of its stream (run `haiku-h2-s0`, stopped in its third). The other routes in the methods table
(MWM, the RRSI Adapter's roles, the AHE Adapter's Evolve Agent and Agent Debugger, EvoSafeHarness's per-role sessions)
ran in the scripted tests only; the Meta-Harness Adapter's proposer on Claude Code has no test of its own (it is the
agent Evolver's session, and, as on Codex, it queues the candidates a failed proposer session left). No limit was met,
so a real refusal and its hold were not seen.
