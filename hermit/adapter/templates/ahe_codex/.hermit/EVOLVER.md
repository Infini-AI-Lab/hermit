# AHE Codex adapter: structure

The adapter is the harness H of Agentic Harness Engineering (arXiv:2604.25850, Sec. 3.1: system prompt, tool
descriptions, tool implementations, middleware, skills, sub-agents, long-term memory) in the layout Codex reads from
its workspace root, with the middleware reaching Codex through its lifecycle hooks (the fixed `hooks.json`, which a
deployment writes as `.codex/hooks.json`; it runs the fixed `hook_runner.py` on every event) and the memory reaching
the model through that middleware:

| path | AHE component | reaches the agent |
|---|---|---|
| `AGENTS.md` (`ADAPTER.md` copy) | system prompt (seed rules non-deletable) + tool-usage guidance (Codex's tool schemas are fixed) | every turn, injected whole |
| `MEMORY.md` | long-term memory | at session start, through `middleware.py`'s `session_start` |
| `middleware.py` | middleware: `before_tool` (veto with a reason), `after_tool` (append a note), `session_start`, `stop`, `before_model` (Codex only, best effort) | inside the tool loop through the host's hooks; per-session state; `hook-calls.jsonl` in every trajectory |
| `scripts/<name>.py` | tool implementation | when the agent runs `python3 scripts/<name>.py` from the adapter's folder |
| `.agents/skills/<name>/SKILL.md` (+ `scripts/`) | skill | name/description always; body on demand; a helper runs as `python3 .agents/skills/<name>/scripts/<file>` from the adapter's folder |
| `agents/<name>.md` | sub-agent (Codex agent role) | when the agent spawns it; `<name>` starts with a letter, at most 64 characters of `[a-z0-9_-]`, unique once a `-` is `_` |

- Learnable: the paths above, and `ADAPTER.md`, which must be a copy of `AGENTS.md` (`check.py` enforces it; the
  Evolver syncs it after each step). A file on a learnable path is live at the next step: no registration.
- Where the adapter is at run time: a deployment copies the whole adapter to `hermit/ahe-codex/` in the agent's
  workspace (the agent's working directory) and tells the agent, above `AGENTS.md`, that the paths of the
  instructions and of the skills are relative to that folder and that it runs their commands from there. So
  `AGENTS.md` and skills name scripts relative to the adapter's root (`python3 scripts/<name>.py`; spelling out
  `hermit/ahe-codex/scripts/<name>.py` works too); `check.py` verifies that every file an invocation names exists in
  the tree. `inp.adapter_dir` is that copy, where `MEMORY.md` and `scripts/` are.
- Fixed: `check.py`, this guide, `exports.yaml`, `hooks.json`, `hook_runner.py`, `hermit_hooks.py`, `README.md`,
  and the placeholders `scripts/README.md`, `.agents/skills/README.md`, `agents/README.md`.

## The middleware cookbook

`middleware.py` defines all five hooks (`hooks.json` starts the runner on the event of each, and the runner calls
the function; an unused one returns `None`), each `(inp, state) -> HookResult | dict | None`. On the sixth event,
`UserPromptSubmit`, the runner calls nothing: it keeps the session's first prompt and hands queued context over. The
runner (`hook_runner.py`, which Codex runs from the adapter's copy on every event) loads the module alone, binds the
session's `state`, calls the hook, persists the state and prints the host's wire. Import the interface as
`from hermit_hooks import HookInput, HookResult` (`hermit_hooks.py`, beside the runner) and otherwise only the
stdlib: nothing else is installed where the agent runs.

| hook | fires | `inp` carries | return |
|---|---|---|---|
| `session_start` | once, before the first model call | `adapter_dir`, `cwd`, `session_id`, `task_text` (when the workspace has a `task.md`; else still `""`: the prompt is submitted after the session starts) | `HookResult(context=...)`: text the model reads first |
| `before_tool` | before every tool call | `tool_kind` (`shell` / `patch` / `read` / `write` / `mcp` / `other`), `tool_name`, `tool_input`, `command` (shell only) | `HookResult(decision="deny", reason=...)`: the command is not run and the model sees the reason; `decision="allow"` or None: runs; `context=` adds a note; `updated_input=` best effort |
| `after_tool` | after every tool call | the above plus `tool_output` / `output_text` (Codex: a plain string), `exit_code` (apply_patch only; the shell wire has none: parse the output) | `HookResult(context=...)`: a note inserted right before the tool output (AHE's "Execution note:"); `decision="block", reason=...`: the reason REPLACES the tool output |
| `stop` | when the model wants to finish | `last_message` | `HookResult(decision="block", reason=...)`: the model continues with the reason as a user message, once (the runner ignores a second block in the same turn) |
| `before_model` | before every model request (Codex only) | `turn_id`, `model` | its output is ignored by Codex: call `inp.queue_context(text)` instead; the queue is flushed into the next `after_tool` / `stop` context |

- `state` is a dict per session, persisted between calls (`hook-state/<session_id>.json` beside the audit, locked:
  tool calls can run concurrently; sub-agents share the parent's session). Keep counters, emitted-once keys, guard
  targets there; keys starting with `_` are the runner's. Keep it JSON-serialisable.
- `inp.task_text` is the task instruction (AHE's contract-inference input): the text of `task.md` in the session's
  workspace (`inp.cwd`) when it has one, read at every call. A task that writes its question there gives the agent a
  prompt that only names the file: on OfficeQA the prompt says "Read `task.md`, ...", and `task.md` holds the
  question, its oracle pages, the corpus note, the candidate files and the source hints. Without a `task.md`, it is
  the prompt of the session's first `UserPromptSubmit`, which the runner keeps in the state (`_prompt`); it is then
  empty in `session_start`, which Codex fires before the prompt is submitted. `inp.adapter_dir` is the adapter's copy
  in the workspace, where `MEMORY.md` and `scripts/` live; `inp.cwd` is the task's working directory.
- Fail open: an exception or a hook slower than the hook timeout (30 s; the runner's own alarm fires 2 s before
  Codex's) produces no output and an `error` line in the audit log, and the tool call proceeds. A middleware can
  slow every tool call down but never abort a trial. Keep hooks fast: no network, no subprocess, no sleeping on I/O.
- Return `None` for "nothing": the seed's four inert hooks are the pattern. A dict with HookResult keys is also
  accepted (`{"decision": "deny", "reason": "..."}`).
- After changing the middleware, read `hook-calls.jsonl` in the next step's trajectories (one line per call: event,
  tool, command, decision, reason, context size, milliseconds, error) before concluding anything about it, exactly
  as AHE's Evolve Agent reads `nexau.txt`: a hook that never fires, always errors, or denies the wrong commands is
  visible there and nowhere else. `hook-state/<session_id>.json` beside it holds the final session states. (The
  runner writes both under `.hermit-hooks/` in the session's workspace; each rollout's trajectory carries them at
  its root, beside `agent-codex.jsonl`.)
- A veto's reason is what the model reads: say what to do instead, not only what was blocked. The same holds for
  an `after_tool` note: one or two lines, specific to the command that just ran.

## What `check.py` enforces

1. Nothing off the surface (the table above); the fixed files and the placeholders are not part of the tree.
2. `AGENTS.md` is never empty, fits the 32,768 bytes Codex reads once deployed (the check deploys a copy of the
   adapter as the framework does and measures the result: the deployment adds its managed block, a note on the
   adapter's folder, a list of the adapter's other files and two bytes per nested heading, so the cap is 32,000
   bytes or 32,768 minus that, whichever is lower; a deployment past the limit is refused), and still contains the
   three seed rules of the paper's system prompt (whitespace-normalised): add to the prompt, never delete or reword
   them.
3. `ADAPTER.md` equals `AGENTS.md`.
4. `MEMORY.md` is present and starts with `# Long-term memory`; the entries go under `## Agent Added Memories`
   (bullets, prose or headings: anything but a blank section).
5. `middleware.py` compiles; it defines all five hooks, each with exactly the two parameters `(inp, state)`, and its
   other top-level functions are private helpers named `_...`; it imports only the stdlib or `hermit_hooks`; none
   of `socket`, `http`, `urllib`, `ssl`, `ftplib`, `smtplib`, `xmlrpc`, `asyncio`, `subprocess`, `ctypes`,
   `multiprocessing`, `pty`, `importlib`, and no call to `__import__`, `importlib.import_module`, `os.system`,
   `os.popen`, `os.fork*`, `os.exec*`, `os.spawn*`, `os.posix_spawn*` (a static check: where the agent runs the
   runner's only guards are its alarm and fail-open); and `session_start`, or a helper it calls, still reads
   `MEMORY.md` (the memory channel is never removed; a comment or docstring does not count). A module that passes
   these rules is then run: every hook once, through `hook_runner.py` as Codex calls it, on a synthetic event with an
   empty state; a module that raises there, returns something other than `None`, a `HookResult` or a dict of its
   keys, or outlasts the runner's alarm fails, with the hook named. Last, `session_start` runs on a copy of the
   adapter whose `MEMORY.md` holds one entry, and fails the check unless that entry reaches the model as context.
6. Every `.py` under `scripts/` (`scripts/<name>.py`, any name) and under a skill's `scripts/` compiles; a
   `python3 scripts/<x>.py` (or `python3 hermit/ahe-codex/scripts/<x>.py`) or `.agents/skills/<n>/scripts/<x>` that
   `AGENTS.md` or a `SKILL.md` invokes exists, named as a deployment lets the agent run it.
7. A skill's frontmatter has `description:` and, if present, `name:` equal to its directory; an agent file's
   frontmatter has `name:` (equal to the file stem) and `description:`, and the stem is a Codex agent name (a
   letter first, at most 64 characters of `[a-z0-9_-]`) that no other agent file takes once a `-` is `_`.
8. An adapter the check cannot see never passes (the sandbox guard).
