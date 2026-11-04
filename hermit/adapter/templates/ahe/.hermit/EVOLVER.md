# AHE adapter: structure

The adapter is the harness H of Agentic Harness Engineering (arXiv:2604.25850, Sec. 3.1: system prompt, tool
descriptions, tool implementations, middleware, skills, sub-agents, long-term memory), the same files for every host
that runs its hooks (Codex, Claude Code), with the middleware reaching the agent through the host's lifecycle hooks
(the fixed `hooks.json`, which a deployment installs in the host's own place; it runs the fixed `hook_runner.py` on
every event) and the memory reaching the model through that middleware:

| path | AHE component | reaches the agent |
|---|---|---|
| `AGENTS.md` (`ADAPTER.md` copy) | system prompt (seed rules non-deletable) + tool-usage guidance (the host's tool schemas are fixed) | every turn, injected whole (Codex reads it as `AGENTS.md`, Claude Code as `CLAUDE.md`) |
| `MEMORY.md` | long-term memory | at session start, through `middleware.py`'s `session_start` |
| `middleware.py` | middleware: `before_tool` (veto with a reason), `after_tool` (append a note), `session_start`, `stop`, `before_model` (queue a note) | inside the tool loop through the host's hooks; per-session state; `hook-calls.jsonl` in every trajectory |
| `scripts/<name>.py` | tool implementation | when the agent runs `python3 scripts/<name>.py` from the adapter's folder |
| `.agents/skills/<name>/SKILL.md` (+ `scripts/`) | skill | name/description always; body on demand; a helper runs as `python3 .agents/skills/<name>/scripts/<file>` from the adapter's folder |
| `agents/<name>.md` | sub-agent (a role of the host) | when the agent spawns it; `<name>` starts with a letter, at most 64 characters of `[a-z0-9_-]`, unique when `-` and `_` are the same (Codex spells a role with `_`, Claude Code with `-`) |

- Learnable: the paths above, and `ADAPTER.md`, which must be a copy of `AGENTS.md` (`check.py` enforces it; the
  Evolver syncs it after each step). A file on a learnable path is live at the next step: no registration.
- Where the adapter is at run time: a deployment copies the whole adapter to `hermit/ahe/` in the agent's
  workspace (the agent's working directory) and tells the agent, above `AGENTS.md`, that the paths of the
  instructions and of the skills are relative to that folder and that it runs their commands from there. So
  `AGENTS.md` and skills name scripts relative to the adapter's root (`python3 scripts/<name>.py`; spelling out
  `hermit/ahe/scripts/<name>.py` works too); `check.py` verifies that every file an invocation names exists in
  the tree. `inp.adapter_dir` is that copy, where `MEMORY.md` and `scripts/` are.
- Fixed: `check.py`, this guide, `exports.yaml`, `hooks.json`, `hook_runner.py`, `hermit_hooks.py` (the
  middleware's interface), `hook_hosts.py` (each host's wire), `README.md`, and the placeholders `scripts/README.md`,
  `.agents/skills/README.md`, `agents/README.md`.

## Hosts

`middleware.py` is written once, against `hermit_hooks.py`: the same `HookInput` in, the same `HookResult` out, on
every host. Which host runs a session is the deployment's to say (the runner reads it from the deployment's record),
and `hook_hosts.py` turns that host's events into the five hooks and the hooks' results into what the host reads.
The hosts differ where the table says; a middleware that keeps to the cookbook below works on both, and one that
needs a host's own behaviour can read `inp.host` (`codex` or `claude-code`). Pi runs no hooks: its deployment lists
the hook commands in `AGENTS.md` for the agent to run by hand, and run so, with no event's payload (`inp.host` is
`pi`), only `session_start` has something to do: it hands the memory over.

<!-- hook_hosts.capability_table: begin -->
| | Codex | Claude Code |
|---|---|---|
| `session_start` | SessionStart, before the prompt (again on a resumed session: `inp.source`); `context` is read before the first model request | SessionStart, before the prompt (`inp.source`: startup, resume ...); `context` is read before the first model request |
| `before_tool` | PreToolUse (none for a `write_stdin` poll): `deny` (not run; the model reads `reason`), `allow`, `updated_input` (run instead); `context` is read with the call's result, but dropped when given with `allow` and no `updated_input` | PreToolUse (none for a call whose input the CLI refused): `deny` (not run; the model reads `reason`), `allow`, `updated_input` (run instead; without `allow` the session's permission mode decides); `context` is read with the call's result |
| `after_tool` | PostToolUse after a call that ran, a failed command too (no exit code: `failed` is None); `output_text` may be the command's last output chunk only (always for one that outlives its first wait: its polls fire nothing); none after a denied call, a failed `apply_patch` or one run through the shell; `block`: the model reads `reason` instead of the output; `context`: beside it. With a code-mode model (`gpt-5.6-luna`) a block or a deny reaches the model as an error in its script | PostToolUse, or PostToolUseFailure after a call that failed (`failed`; `exit_code` and the error as `output_text`); `output_text` is None after a Write, Edit, MultiEdit or NotebookEdit (no output text); a command moved to the background at its timeout is a PostToolUse with the output so far; none after a denied call or one whose input the CLI refused; `block`: the output stays and the model also reads `reason`; `context`: beside it |
| `stop` | Stop: `block` once per turn, the model going on with `reason`; a blocked stop's `context` and the queue go with the reason | Stop: `block` once per turn, the model going on with `reason`; a stop's `context` goes out only with a block (alone, it would make the model go on); a sub-agent's end is no stop |
| `before_model` | PreModelRequest, before every model request; its output is ignored: only its `context` is queued | no such event: the runner calls it after the prompt, after every model message's tool calls (PostToolBatch) and after a blocked stop, once per model request (none before a sub-agent's first); only its `context` is queued |
| queued context | handed over with the next prompt or tool result, or a blocked stop; a stop that goes through leaves it for the session's next prompt (a resumed session) | handed over with the agent's next prompt or tool result (a failed one too), or a blocked stop; a stop that goes through leaves it for the next prompt; a sub-agent's queue is its own |
| `stop_session` | ends the session at `session_start` only; elsewhere the session goes on (with a classic, not code-mode, model a stop at `after_tool` replaces the output with `reason`) | ends the session at `before_tool` (once the call has run) and at `after_tool` of a call that succeeded; ignored at `session_start` and at `after_tool` of a failed call |
| `system_message` | shown nowhere | shown to the person watching, not to the model |
| `inp.tool_kind` | `shell` (Bash: a code-mode script's `exec_command` too), `patch` (`apply_patch`), `mcp`, `other` | `shell` (Bash), `read` (Read), `write` (Write, Edit, MultiEdit, NotebookEdit), `mcp`, `other` (Grep, Glob, Agent ...) |
| `inp.cwd`, `inp.model`, `inp.turn_id` | the session's directory (`inp.workspace`), even for a command run in another `workdir`; the model; the turn | the shell's current directory, which follows the agent's `cd` (`inp.workspace` is the project's); no model; the prompt's id |
<!-- hook_hosts.capability_table: end -->

## The middleware cookbook

`middleware.py` defines all five hooks (`hooks.json` starts the runner on the host's events, and the runner calls
the function of each; an unused one returns `None`), each `(inp, state) -> HookResult | dict | None`. On
`UserPromptSubmit` the runner calls no function of its own: it keeps the session's first prompt and hands queued
context over. The runner (`hook_runner.py`, which the host runs from the adapter's copy on every event) loads the
module alone, binds the session's `state`, calls the hook, persists the state and prints the host's output. Import
the interface as `from hermit_hooks import HookInput, HookResult` (`hermit_hooks.py`, beside the runner) and otherwise
only the stdlib: nothing else is installed where the agent runs.

| hook | fires | `inp` carries | return |
|---|---|---|---|
| `session_start` | once, before the first model call | `adapter_dir`, `workspace`, `session_id`, `task_text` (when the workspace has a `task.md`; else still `""`: the prompt is submitted after the session starts) | `HookResult(context=...)`: text the model reads first |
| `before_tool` | before a tool call (the host table names the calls with none) | `tool_kind` (`shell` / `patch` / `read` / `write` / `mcp` / `other`), `tool_name` (the host's own), `tool_input`, `command` (shell only) | `HookResult(decision="deny", reason=...)`: the call is not run and the model sees the reason; `decision="allow"` or None: runs; `context=` adds a note the model reads with the call's result (without a decision: Codex drops one given with `allow`); `updated_input=`: the call runs with this input instead |
| `after_tool` | after a tool call (the host table names the calls with none) | the above plus `tool_output` (the host's own), `output_text` (the output, or a failed call's error; None where the host reports no output text), `exit_code` and `failed` when the host reports them (else None: parse `output_text`), `duration_ms` | `HookResult(context=...)`: a note beside the tool output (AHE's "Execution note:"); `decision="block", reason=...`: the reason reaches the model with or instead of the output (the host table) |
| `stop` | when the model wants to finish | `last_message` | `HookResult(decision="block", reason=...)`: the model continues with the reason as a user message, once (the runner ignores a second block in the same turn); a `context` goes out only with a block |
| `before_model` | before a model request (on a host without the event, where the runner knows one follows: the host table) | `session_id`, `model` and `turn_id` when the host sends them | no host reads its output: call `inp.queue_context(text)` or return `HookResult(context=...)`, both queued for the next tool result or prompt, or a blocked stop |

- `state` is a dict per session, persisted between calls (`hook-state/<session_id>.json` beside the audit, locked:
  tool calls can run concurrently; sub-agents share the parent's session). Keep counters, emitted-once keys, guard
  targets there; keys starting with `_` are the runner's. Keep it JSON-serialisable.
- `inp.task_text` is the task instruction (AHE's contract-inference input): the text of `task.md` in the session's
  workspace (`inp.workspace`) when it has one, read at every call. A task that writes its question there gives the
  agent a prompt that only names the file: on OfficeQA the prompt says "Read `task.md`, ...", and `task.md` holds
  the question, its oracle pages, the corpus note, the candidate files and the source hints. Without a `task.md`, it
  is the prompt of the session's first `UserPromptSubmit`, which the runner keeps in the state (`_prompt`); it is
  then empty in `session_start`, which the hosts fire before the prompt is submitted. `inp.adapter_dir` is the
  adapter's copy in the workspace, where `MEMORY.md` and `scripts/` live; `inp.workspace` is the session's workspace,
  the same for the whole session; `inp.cwd` is the host's working directory for the event (the host table).
- Fail open: an exception or a hook slower than the hook timeout (30 s; the runner's own alarm fires 2 s before the
  host's) produces no output and an `error` line in the audit log, and the tool call proceeds. A middleware can
  slow every tool call down but never abort a trial. Keep hooks fast: no network, no subprocess, no sleeping on I/O.
- Return `None` for "nothing": the seed's four inert hooks are the pattern. A dict with HookResult keys is also
  accepted (`{"decision": "deny", "reason": "..."}`). A `deny` or a `block` needs a non-empty `reason`: without one it
  is an error, failing open as above. `stop_session=True` ends the session only where the host lets a hook end it,
  and `system_message` never reaches the model (the host table): neither is a way to steer the agent.
- After changing the middleware, read `hook-calls.jsonl` in the next step's trajectories (one line per call: event,
  tool, command, decision, reason, context size, milliseconds, error; a `before_model` the runner called carries
  `at`, the event it followed) before concluding anything about it, exactly as AHE's Evolve Agent reads
  `nexau.txt`: a hook that never fires, always errors, or denies the wrong commands is visible there and nowhere
  else. `hook-state/<session_id>.json` beside it holds the final session states. (The runner writes both under
  `.hermit-hooks/` in the session's workspace; each rollout's trajectory carries them at its root, beside the
  agent's event stream: `agent-codex.jsonl` from Codex, `agent-claude.jsonl` from Claude Code.)
- A veto's reason is what the model reads: say what to do instead, not only what was blocked. The same holds for
  an `after_tool` note: one or two lines, specific to the command that just ran.

## What `check.py` enforces

The check deploys a copy of the adapter to every host as the framework does (Codex, Claude Code) and holds what each
host gets to the rules below.

1. Nothing off the surface (the table above); the fixed files and the placeholders are not part of the tree.
2. `AGENTS.md` is never empty, fits what every host reads once deployed (the check measures each host's context
   file: the deployment adds its managed block, a note on the adapter's folder, a list of the adapter's other files
   and two bytes per nested heading; on Codex, which reads 32,768 bytes, the cap is 32,000 bytes or 32,768 minus
   that, whichever is lower, and a deployment past the limit is refused), and still contains the three seed rules of
   the paper's system prompt (whitespace-normalised): add to the prompt, never delete or reword them.
3. `ADAPTER.md` equals `AGENTS.md`.
4. `MEMORY.md` is present and starts with `# Long-term memory`; the entries go under `## Agent Added Memories`
   (bullets, prose or headings: anything but a blank section).
5. `middleware.py` compiles; it defines all five hooks, each with exactly the two parameters `(inp, state)`, and its
   other top-level functions are private helpers named `_...`; it imports only the stdlib or `hermit_hooks`; none
   of `socket`, `http`, `urllib`, `ssl`, `ftplib`, `smtplib`, `xmlrpc`, `asyncio`, `subprocess`, `ctypes`,
   `multiprocessing`, `pty`, `importlib`, `posix`, `nt`, `_posixsubprocess`, no `from os import *`, and no call to
   `__import__`, `importlib.import_module`, `os.system`, `os.popen`, `os.fork*`, `os.exec*`, `os.spawn*`,
   `os.posix_spawn*`, under these names or an import's alias (a static check: where the agent runs the runner's only
   guards are its alarm and fail-open). A module that passes these rules is then run on every host: one session of
   synthetic events in the host's own shape and order (the prompt, two tool calls and their results, a stop and the
   stop after it), each through the hook the deployment installed, as the host runs it (the deployed command through
   a shell), the state and the queue carried from one event to the next; a module that raises there, at its first
   call or a later one, returns something other than `None`, a `HookResult` or a dict of its keys, outlasts the
   runner's alarm, or makes an output with a field the host does not read or with the one drop the check knows of (a
   context given with `allow` on Codex) fails, with the hook, the event and the host named, and so do hooks slower
   than 75 s for all the runs together; the host table's other limits are not checked. Last, `session_start` runs on
   a fresh deployed copy whose `MEMORY.md` holds one entry, on every host, and fails the check unless that entry
   reaches the model as context (the memory channel is never removed).
6. Every `.py` under `scripts/` (`scripts/<name>.py`, any name) and under a skill's `scripts/` compiles; a
   `python3 scripts/<x>.py` (or `python3 hermit/ahe/scripts/<x>.py`) or `.agents/skills/<n>/scripts/<x>` that
   `AGENTS.md` or a `SKILL.md` invokes exists, named as a deployment lets the agent run it.
7. A skill's frontmatter (read as YAML, as the hosts read it) has `description:` and, if present, `name:` equal to
   its directory; an agent file's frontmatter has `name:` (equal to the file stem) and `description:`, and the stem
   is a role name on every host (a letter first, at most 64 characters of `[a-z0-9_-]`) that no other agent file
   takes when `-` and `_` are the same.
8. An adapter the check cannot see never passes (the sandbox guard).
