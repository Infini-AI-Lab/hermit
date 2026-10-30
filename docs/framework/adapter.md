# Adapter

An adapter is a directory of source files governed by its template's metadata in `.hermit/`. The Trainer stores
versions of those files in Git. The acting integration turns their exports into host instructions, skills, roles, and callable tools.

There are two file paths to understand: **deployment creates files for the acting host; evolution edits a working
copy of the adapter sources**.

```{raw} html
:file: ../_includes/adapter-contract.html
```

## The adapter structure

The complete example in `docs/_examples/file_adapter/` contains:

```text
file_adapter/
  .hermit/
    adapter.yaml               required: template identity and specification
    EVOLVER.md                 template guide for the learner
  exports.yaml                 optional: what the acting agent receives
  ADAPTER.md                   optional: context for the acting agent
  agents/reviewer.md           optional: one file per subagent
  skills/verify-output/
    SKILL.md                   optional: one skill
  tools.py                     optional: tool implementations (the tool list is in exports.yaml)
  README.md                    usage and dependencies
```

Every adapter has `.hermit/adapter.yaml` (its sections and rules: [adapter metadata reference](adapter-manifest.md)).
`ADAPTER.md` is a fixed name and is always exported as the acting agent's context; without it the context is empty.
`exports.yaml` declares the other exports; without it the adapter exports its context only, and its `context` key may
be omitted. `.hermit/EVOLVER.md` is learner guidance; it is not inserted into the acting agent's context. `tools.py` is a convention:
each tool's `entrypoint` names its file and function.

A subagent is one Markdown file, in the same format as Claude Code's subagent files: YAML frontmatter with `name`
and `description`, and the body as its instructions.

```{literalinclude} ../_examples/file_adapter/agents/reviewer.md
:language: markdown
```

These declarations connect the subagent and skill directories to the loader:

```{literalinclude} ../_examples/file_adapter/exports.yaml
:language: yaml
:end-before: tools:
```

## Acting interface

### Which files are written into the host?

The connector reads `exports.yaml` and renders or copies its exports into a **separate host workspace**:

| Adapter source | Deployment operation | Codex output | Claude Code output | Pi output |
|---|---|---|---|---|
| `ADAPTER.md` (always exported) | Insert its text into a managed instruction block. | `AGENTS.md` | `CLAUDE.md` | `AGENTS.md` |
| `skills/verify-output/` | Copy the skill directory. | `.codex/skills/verify-output/` | `.claude/skills/verify-output/` | `.pi/skills/verify-output/` |
| `agents/reviewer.md`, through `agents` in `exports.yaml` | Convert the subagent into native host files: frontmatter gives name and description, the body becomes its instructions. | `.codex/agents/reviewer.toml`, registered in `.codex/config.toml` | `.claude/agents/reviewer.md` | refused: Pi has no subagents |
| `tools.py`, through `tools` in `exports.yaml` | Configure a framework MCP server that loads the source adapter. | `.codex/config.toml` | `.mcp.json` | `.pi/mcp.json` |

This is how the adapter reaches the acting system. The host reads the generated instruction, skill, and role files;
tool configuration makes the declared tools available through its tool interface. Merely placing the adapter
directory next to a task does not make an agent use it.

For the example above, the generated workspaces contain:

```text
codex-workspace/
  AGENTS.md
  .codex/
    config.toml
    agents/reviewer.toml
    skills/verify-output/SKILL.md
  .hermit-deploy.json
```

```text
claude-workspace/
  CLAUDE.md
  .mcp.json
  .claude/
    agents/reviewer.md
    skills/verify-output/SKILL.md
  .hermit-deploy.json
```

```text
pi-workspace/            # only for an adapter without roles
  AGENTS.md
  .pi/
    mcp.json
    skills/verify-output/SKILL.md
  .hermit-deploy.json
```

Only declared exports produce corresponding content. A prompt-only adapter has no tool-server entry, skills, or
roles to install. The generated deployment report identifies the actual paths and role names.

### Context is inserted into project instructions

`ADAPTER.md` is the source. `AGENTS.md` or `CLAUDE.md` is the host-facing result. The connector inserts the
rendered context between markers like these:

```html
<!-- hermit:file-example:start -->
<!-- Rendered adapter instructions appear in this managed block. -->
<!-- hermit:file-example:end -->
```

The snippet shows the boundaries, not the complete generated contents. Redeployment replaces that adapter's block
and preserves project text outside it. Existing adapter headings are nested under the generated section title.

The deployment command prepares a workspace; it does not launch the agent. Its output must be empty or a workspace
previously generated for the same host. The source adapter and output workspace cannot contain one another.
The generated `.codex/config.toml`, `.mcp.json` or `.pi/mcp.json` is rewritten on deployment; it is not a general configuration
merger. Global host configuration is not modified.

### Tools run from the adapter source

Tool code is not copied into `AGENTS.md`. The generated MCP configuration starts a framework wrapper with the
adapter's absolute path:

```text
python -m hermit.runtime.mcp_server --adapter /absolute/path/to/adapter ...
```

That wrapper exposes the declared tool schemas, loads the Python functions, and returns their results to the
acting agent. The source adapter and its runtime dependencies must remain available.

Each tool declaration names an `entrypoint` such as `tools.py:count_words` and object input/output JSON Schemas.
The Python function may be synchronous or asynchronous. Subagents can also come from a Python exporter that returns
the list instead of a directory of files; the context is always the `ADAPTER.md` file.

An independent service is another integration option. The framework service can host adapter tools, shared state,
and reaction logic with its own lifecycle. The pipeline must start it and integrate any control channel with the
acting host. Ordinary `deploy` does not start that service.

### Per-task context

`exports.yaml` may name one of its declared tools as `context_tool`. That tool is the harness's: it is called
once per task, before the agent's first model call, with `{query, task}` (the task's input text and its row), and
the `text` it returns is appended below `ADAPTER.md` for that task. The agent is never offered it: `Session.tools`,
`tool_specs` and `respond` leave it out. `Session.context(query=..., task=...)` makes the call; the built-in `react`
agent passes the task's user message as the query and records the tool's whole result in
`RunOutput.extra["context_tool"]`, so the learning material says what the agent read. A workspace or a plugin is
installed once and has no per-task moment, so a template whose adapters declare a context tool sets
[`plugin-portable: false`](adapter-manifest.md#plugin-portable), and `deploy` and `convert` refuse them. The `reharness` template
retrieves reflections this way ([ReHarness](../methods/reharness.md)); the declaration is in the
[manifest reference](adapter-manifest.md#per-task-context).

### Custom acting pipelines

A program that runs its own agent uses an adapter through `Session`, the Python counterpart of `deploy` and
`convert`:

```python
from hermit import Session

session = Session("docs/_examples/file_adapter")
system_prompt = base_prompt + session.context(query=question, task=task)   # ADAPTER.md, plus a context_tool's text
tools = task_tools + session.tool_specs("openai")        # or "anthropic"; the adapter's tools beside the task's
...
for call in message["tool_calls"]:                       # a chat-completions turn
    if session.has_tool(call["function"]["name"]):
        messages.append(session.respond(call))            # the tool message; errors come back as text
```

`session.context()` without arguments is `ADAPTER.md` alone, as `deploy` renders it; with `query` and `task`, an
adapter whose manifest names a [`context_tool`](#per-task-context) gets that tool's text appended, and
`session.last_context` holds the tool's whole result for `RunOutput.extra["context_tool"]`.
`session.call(name, arguments)` runs one tool synchronously (also inside a running event loop), `acall` is the
coroutine, `agents()` and `skills()` give the other exports. Resources a tool may receive through its reserved
`_resources` parameter are bound at construction: `deployment=` (a host.json or its dict), `learning_material=`,
`evidence=` (a record directory), or any mapping under `resources=`. `audit_log=` writes one JSON line per call,
the same record `hermit serve` writes; the log must live outside the adapter directory, whose files the Trainer
keeps under version control, and `Session` refuses a path inside it. The [task interface](task.md) leaves host
integration to the pipeline; it does not automatically implement every export.

Outside a run's rollout sandbox (a production program, say), `Session(adapter, sandbox=True)` keeps the adapter's code
out of the program: the tools run in a `hermit serve` inside a sandbox (no network, the adapter read-only,
nothing writable but the evidence store), reached over MCP, and the audit log is still written by the program. Pass a
configured `hermit.sandbox.Sandbox` instead of `True` to grant more. `deployment`, `learning_material` and
`evidence` go with it; other `resources` are refused, and so is an adapter whose subagents come from a Python exporter.
Close the session, or use it in a `with` block, to stop the server.

## Evolution interface

### What does the Evolver edit?

At the start of each step, the Trainer exports the accepted Git version into ordinary working files. The acting
batch and the learner receive separate copies:

| Location in a run | Purpose | Who changes it? |
|---|---|---|
| `root/` | Template reference for the specification. | Prepared by the Trainer. |
| `rollout_adapter/` | Adapter supplied to the current acting batch. | Prepared by the Trainer; the pipeline treats it as input. |
| `workspace/adapter/` | Candidate files being learned. | Evolver edits these files. |
| `checkout/` | Git worktree holding the recorded adapter version. | Trainer updates and commits it. |
| `workspace/state/` | Method-specific search or learning state. | Evolver; separate from the adapter. |

The learner's path is `ctx.workspace.adapter`. A scripted Evolver can use normal file operations; a learner agent
can edit the same files through its file tools. For example, this method appends a predefined instruction:

```python
from hermit.evolver.base import StepContext, StepResult

def step(self, ctx: StepContext) -> StepResult:
    path = ctx.workspace.adapter / "ADAPTER.md"
    path.write_text(path.read_text() + "\nCheck the result against the task requirements.\n")
    return StepResult(status="done", summary="Added a result-checking instruction")
```

This demonstrates the file handoff, not a learning strategy. A real method chooses its edits from the available
evidence. It leaves the selected candidate in the directory and returns status and usage through `StepResult`.

The Evolver edits `ADAPTER.md` or other permitted source files. It does not edit the acting workspace's generated
`AGENTS.md` as its learned artifact. After acceptance, a later pipeline execution loads or redeploys the new source
version. An already-running host does not automatically receive a live update.

### How does the specification limit edits?

The template declares the writable content and fixed content. Here is the example's actual specification:

```{literalinclude} ../_examples/file_adapter/.hermit/adapter.yaml
:language: yaml
:start-at: structure:
```

For this template:

| Proposed edit | Result |
|---|---|
| Rewrite `ADAPTER.md`. | Allowed, within its format and size limit. |
| Edit `agents/reviewer.md`. | Allowed if its frontmatter keeps `name` and `description`; the next deployment uses the new instructions. |
| Add or edit a skill under `skills/`. | Allowed, within the declared file and size limits. |
| Edit `tools.py`. | Rejected: it is fixed here. |
| Delete `ADAPTER.md`. | Allowed: the adapter then exports no context. |
| Add an undeclared file elsewhere in the adapter. | Rejected: it is outside the learnable paths. |
| Change the tool declarations in `exports.yaml`. | Allowed, if the file parses (check 3) and the tools still load (check 5). |
| Edit `.hermit/adapter.yaml` or `.hermit/EVOLVER.md`. | Rejected: `.hermit/` is the template's; repair restores it. |

Another template can permit tool-code changes or other learnable files. The scope comes from that template;
it is not a hard-coded restriction to prompts.

The candidate directory is writable, but not every edit is acceptable. Fixed-file protection is enforced by the
specification check after the learner returns, rather than by making each fixed file physically read-only inside
the working copy. The template's `.hermit/EVOLVER.md` explains the intended editing rules to the learner.

### Who checks and records the candidate?

The Trainer compares changed files with the template specification. It checks `.hermit/` and fixed paths, allowed
paths, that `exports.yaml` parses, formats, and export loading. If a candidate violates the specification, it records the invalid version,
restores affected files from the template or previous adapter, checks again, and records the repair.
The next batch uses the accepted version. The learner sees any repair feedback in its next learning material.

By default a learner may change everything in the adapter except `.git/` and `.hermit/`. Every other limit is added
by the template (`structure` and its `check`) or by the Evolver itself. In MWM, for example, the Evolver protects
`interface.py` and `mwm_core/`, and the template's check keeps the `wm` tool in `exports.yaml`. `.hermit/` is carried byte-for-byte as the tree's root has it, except
`.hermit/CARD.md`, which only the Trainer and `hermit eval` write. `structure` and `check` are always read from the
template root. See [failure handling](trainer.md#when-the-binding-check-fails).

## Specification check

The callable interface is `check_spec(adapter, root) -> CheckResult`. The result contains `passed` and issues
naming the affected path, check, and problem. It establishes conformance and loadability; it does not measure task
performance.

The built-in load check renders context, loads role declarations, and imports declared tools. It does not launch
the acting host or service. Additional runtime requirements belong in the template's custom check or task setup.

A template may add a checker for requirements not expressed by `structure`:

```python
from pathlib import Path

def check(adapter_path: str) -> list[dict]:
    if not (Path(adapter_path) / "ADAPTER.md").read_text().strip():
        return [{"path": "ADAPTER.md", "problem": "instructions must not be empty"}]
    return []
```

Save it as `.hermit/check.py` and declare it as `check: .hermit/check.py:check`. The checker is loaded from the
template root. Return a nonempty issue list
to reject a candidate; a dictionary with an `issues` list is also accepted. It runs in a subprocess with a
120-second limit and network isolation when bubblewrap is available.

## Minimal example

From the repository root after installation, use fresh output directories:

```bash
mkdir -p .demo
cp -R docs/_examples/file_adapter .demo/file-adapter
hermit deploy --adapter .demo/file-adapter --workspace .demo/file-codex --host codex
hermit deploy --adapter .demo/file-adapter --workspace .demo/file-claude --host claude-code
```

Inspect `.demo/file-codex/AGENTS.md`, `.demo/file-claude/CLAUDE.md`, and the generated roles, skills, and tool
configuration. No model is called by these deployment commands.

After editing the source copy, check it against the template:

```bash
python -m hermit.spec .demo/file-adapter --root docs/_examples/file_adapter
```

Then rerun the deployment command to refresh host files. This is the same source-edit → check → deploy boundary
used during training, with the Trainer additionally managing candidate acceptance and Git versions.

## Directory, Git version, and runtime state

The portable artifact is the adapter directory. The Trainer keeps a Git tree of its versions, with run branches,
checkpoint tags, and a final tag. The learner's exported working copy is not itself a Git worktree and does not
need a `.git/` directory. The Trainer performs the version-control operations.

Deployment consumes the adapter files. Learner state, execution logs, and a service's per-run state stay outside
the package unless a template explicitly makes some evidence or memory part of its learned content.
`.hermit/CARD.md` records provenance and evaluations; it is not acting context.
