# Adapter

An adapter is a directory of source files governed by `adapter.yaml`. The Trainer stores versions of those files
in Git. The acting integration turns their exports into host instructions, skills, roles, and callable tools.

There are two file paths to understand: **deployment creates files for the acting host; evolution edits a working
copy of the adapter sources**.

```{raw} html
:file: ../_includes/adapter-contract.html
```

## The adapter files

The complete example in `docs/_examples/file_adapter/` contains:

```text
file_adapter/
  adapter.yaml                 required: identity, exports and specification
  ADAPTER.md                   required: context for the acting agent
  agents/reviewer.md           optional: one file per subagent
  skills/verify-output/
    SKILL.md                   optional: one skill
  tools.py                     optional: tool implementations (the tool list is in adapter.yaml)
  EVOLVER.md                   template guide for the learner
  README.md                    usage and dependencies
```

Every adapter has `adapter.yaml` and `ADAPTER.md`. `ADAPTER.md` is a fixed name and is always exported as the acting
agent's context; `exports.context` may be omitted. Everything else is optional and declared in `adapter.yaml`.
`EVOLVER.md` is learner guidance; it is not inserted into the acting agent's context. `tools.py` is a convention:
each tool's `entrypoint` names its file and function.

A subagent is one Markdown file, in the same format as Claude Code's subagent files: YAML frontmatter with `name`
and `description`, and the body as its instructions.

```{literalinclude} ../_examples/file_adapter/agents/reviewer.md
:language: markdown
```

These declarations connect the subagent and skill directories to the loader:

```{literalinclude} ../_examples/file_adapter/adapter.yaml
:language: yaml
:start-at: exports:
:end-before:   tools:
```

## Acting interface

### Which files are written into the host?

The connector reads the adapter manifest and renders or copies its exports into a **separate host workspace**:

| Adapter source | Deployment operation | Codex output | Claude Code output |
|---|---|---|---|
| `ADAPTER.md` (always exported) | Insert its text into a managed instruction block. | `AGENTS.md` | `CLAUDE.md` |
| `skills/verify-output/` | Copy the skill directory. | `.codex/skills/verify-output/` | `.claude/skills/verify-output/` |
| `agents/reviewer.md`, through `exports.agents` | Convert the subagent into native host files: frontmatter gives name and description, the body becomes its instructions. | `.codex/agents/reviewer.toml`, registered in `.codex/config.toml` | `.claude/agents/reviewer.md` |
| `tools.py`, through `exports.tools` | Configure a framework MCP server that loads the source adapter. | `.codex/config.toml` | `.mcp.json` |

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
  .harnessadapter-deploy.json
```

```text
claude-workspace/
  CLAUDE.md
  .mcp.json
  .claude/
    agents/reviewer.md
    skills/verify-output/SKILL.md
  .harnessadapter-deploy.json
```

Only declared exports produce corresponding content. A prompt-only adapter has no tool-server entry, skills, or
roles to install. The generated deployment report identifies the actual paths and role names.

### Context is inserted into project instructions

`ADAPTER.md` is the source. `AGENTS.md` or `CLAUDE.md` is the host-facing result. The connector inserts the
rendered context between markers like these:

```html
<!-- harnessadapter:file-example:start -->
<!-- Rendered adapter instructions appear in this managed block. -->
<!-- harnessadapter:file-example:end -->
```

The snippet shows the boundaries, not the complete generated contents. Redeployment replaces that adapter's block
and preserves project text outside it. Existing adapter headings are nested under the generated section title.

The deployment command prepares a workspace; it does not launch the agent. Its output must be empty or a workspace
previously generated for the same host. The source adapter and output workspace cannot contain one another.
The generated `.codex/config.toml` or `.mcp.json` is rewritten on deployment; it is not a general configuration
merger. Global host configuration is not modified.

### Tools run from the adapter source

Tool code is not copied into `AGENTS.md`. The generated MCP configuration starts a framework wrapper with the
adapter's absolute path:

```text
python -m harnessadapter.runtime.mcp_server --adapter /absolute/path/to/adapter ...
```

That wrapper exposes the declared tool schemas, loads the Python functions, and returns their results to the
acting agent. The source adapter and its runtime dependencies must remain available.

Each tool declaration names an `entrypoint` such as `tools.py:count_words` and object input/output JSON Schemas.
The Python function may be synchronous or asynchronous. Subagents can also come from a Python exporter that returns
the list instead of a directory of files; the context is always the `ADAPTER.md` file.

An independent service is another integration option. The framework service can host adapter tools, shared state,
and reaction logic with its own lifecycle. The pipeline must start it and integrate any control channel with the
acting host. Ordinary `deploy` does not start that service.

### Custom acting pipelines

A custom pipeline may consume exports directly instead of generating host files. For example:

```python
from pathlib import Path
from harnessadapter.adapter.loader import Adapter

adapter = Adapter(Path("docs/_examples/file_adapter"))
instructions = adapter.context()
subagents = adapter.agent_definitions()
```

The pipeline places `instructions` in its agent context and integrates any other exports it needs.
`await adapter.call(name, arguments)` invokes a declared tool with schema validation. Calls may bind framework
resources through a reserved `_resources` parameter. The [task interface](task.md) leaves host integration to the
pipeline; it does not automatically implement every export.

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
from harnessadapter.evolver.base import StepContext, StepResult

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

```{literalinclude} ../_examples/file_adapter/adapter.yaml
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
| Delete `ADAPTER.md`. | Rejected: every adapter has one; repair restores it. |
| Add an undeclared file elsewhere in the adapter. | Rejected: it is outside the learnable paths. |
| Change the tool declarations in `adapter.yaml`. | Rejected: the exports remain fixed against the template. |

Another template can permit tool-code changes or other learnable files. The scope comes from that template;
it is not a hard-coded restriction to prompts.

The candidate directory is writable, but not every edit is acceptable. Fixed-file protection is enforced by the
specification check after the learner returns, rather than by making each fixed file physically read-only inside
the working copy. The template's `EVOLVER.md` explains the intended editing rules to the learner.

### Who checks and records the candidate?

The Trainer compares changed files with the template specification. It checks fixed paths, allowed paths, formats,
manifest sections, and export loading. If a candidate violates the specification, it records the invalid version,
restores affected files from the template or previous adapter, checks again, and records the repair.
The next batch uses the accepted version. The learner sees any repair feedback in its next learning material.

The manifest sections `template`, `exports`, `structure`, `check`, and `evolver_tools` stay fixed. `origin` and the
final `CARD.md` are written by the Trainer. See [failure handling](trainer.md#when-the-binding-check-fails).

## Specification check

The callable interface is `check_spec(adapter, root) -> CheckResult`. The result contains `passed` and issues
naming the affected path, check, and problem. It establishes conformance and loadability; it does not measure task
performance.

The built-in load check renders context, loads role declarations, and imports declared tools. It does not launch
the acting host or service. Additional runtime requirements belong in the template's custom check or task setup.

A template may add a checker for requirements not expressed by the manifest:

```python
from pathlib import Path

def check(adapter_path: str) -> list[dict]:
    if not (Path(adapter_path) / "ADAPTER.md").read_text().strip():
        return [{"path": "ADAPTER.md", "problem": "instructions must not be empty"}]
    return []
```

Declare it as `check: check.py:check`. The checker is loaded from the template root. Return a nonempty issue list
to reject a candidate; a dictionary with an `issues` list is also accepted. It runs in a subprocess with a
120-second limit and network isolation when bubblewrap is available.

## Minimal example

From the repository root after installation, use fresh output directories:

```bash
mkdir -p .demo
cp -R docs/_examples/file_adapter .demo/file-adapter
harnessadapter deploy --adapter .demo/file-adapter --output .demo/file-codex --host codex
harnessadapter deploy --adapter .demo/file-adapter --output .demo/file-claude --host claude-code
```

Inspect `.demo/file-codex/AGENTS.md`, `.demo/file-claude/CLAUDE.md`, and the generated roles, skills, and tool
configuration. No model is called by these deployment commands.

After editing the source copy, check it against the template:

```bash
python -m harnessadapter.spec .demo/file-adapter --root docs/_examples/file_adapter
```

Then rerun the deployment command to refresh host files. This is the same source-edit → check → deploy boundary
used during training, with the Trainer additionally managing candidate acceptance and Git versions.

## Directory, Git version, and runtime state

The portable artifact is the adapter directory. The Trainer keeps a Git tree of its versions, with run branches,
checkpoint tags, and a final tag. The learner's exported working copy is not itself a Git worktree and does not
need a `.git/` directory. The Trainer performs the version-control operations.

Deployment consumes the adapter files. Learner state, execution logs, and a service's per-run state stay outside
the package unless a template explicitly makes some evidence or memory part of its learned content.
`CARD.md` records provenance and evaluations; it is not acting context.
