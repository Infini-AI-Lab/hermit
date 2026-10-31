# Adapter metadata reference

An adapter keeps two kinds of declaration apart. `.hermit/adapter.yaml` is the template's metadata: it names the
template the adapter comes from, states which files a learner may change, and declares the template's own checker
and learner tools. `exports.yaml`, at the adapter root, declares what the adapter exports to an acting agent. The
rules below are the ones `hermit.adapter.manifest` and `hermit.adapter.loader` enforce; metadata or exports that
break one do not load, and a candidate whose `exports.yaml` does not parse fails the specification check.

```text
my_adapter/
  .hermit/
    adapter.yaml      template identity, structure, check, evolver_tools
    check.py          the template's checker, when it has one
    EVOLVER.md        the learner's guide; not exported
    CARD.md           the adapter card, written by the Trainer and `hermit eval`
  exports.yaml        what the acting agent receives
  ADAPTER.md          the acting agent's context
  ...                 skills, roles, tool code, other content
```

`.hermit/adapter.yaml` has four sections and no others:

| Section | Who writes it | Who reads it |
|---|---|---|
| [`template`](#template) | the template author | everyone: identity and compatibility |
| [`structure`](#structure) | the template author | the Trainer's specification check; the learner, through `.hermit/EVOLVER.md` |
| [`check`](#check) | the template author | the specification check |
| [`evolver_tools`](#evolver_tools) | the template author | the Evolver's tool endpoint |

A fifth, [`serve`](#serve-design-not-implemented), is proposed and not yet read by anything; its stdio command,
`hermit serve DIR`, exists and is what `convert` writes into a plugin. Paths in the metadata are relative to the
adapter root, so a checker inside `.hermit/` is declared as `.hermit/check.py:check`.

An acting integration reads `exports.yaml` and the `template` identity; `flatten()` returns the exports with
`context`, `id`, `name` and `version` filled in.

## What a learner may change

By default a learner may change everything in an adapter except `.git/` and `.hermit/`. Every other limit is added
by the template, through [`structure`](#structure) and its `check`, or by the Evolver itself. In MWM, for example,
the Evolver protects `interface.py` and `mwm_core/`, and the template's check keeps the `wm` tool in `exports.yaml`.

`.hermit/` is the same in every adapter of a tree: each carries it byte-for-byte as the tree's root (the template)
does. The one exception is `.hermit/CARD.md`, which only the Trainer and `hermit eval` write; the check compares it
with the parent version instead. Check 1 of the [specification check](adapter.md#specification-check) reports any
change, deletion or addition under `.hermit/`, and repair restores the file from the root (the card from the
parent). Because `structure` and `check` are always read from the template root, an adapter cannot loosen its own
limits.

## Customizing an adapter

Most people never write the metadata from scratch: they take a template and change one thing. A run names a
template either by its built-in id (`template: prompt-only@1.0`) or by a directory holding a
`.hermit/adapter.yaml` (`template: ./my_template`), so the way to customize is to copy a template directory, edit
it, and point the run at the copy. The built-in templates are under `hermit/algorithms/templates/`; finished
adapters with their cards are under `examples/`. What you want to change decides which part you touch:

| You want to | Change | Where |
|---|---|---|
| Different starting instructions, skills or roles | the learnable files themselves: `ADAPTER.md`, `skills/`, `agents/` | no metadata change; the learner starts from your text |
| A new tool for the acting agent | add its Python to the directory and declare it | [`tools` in `exports.yaml`](#tool-declaration); list the file under `fixed` to keep it as written, or as learnable to let the learner change it |
| Context chosen per task (a retrieval, a lookup) | a tool declaration the harness calls, not the agent | [`context_tool` in `exports.yaml`](#per-task-context) |
| Let the learner edit more, or less | the list of learnable paths and their limits (size, file count, frontmatter) | [`structure`](#structure) |
| Keep a file exactly as shipped | list it under `fixed` | [`structure`](#structure) |
| Keep the exports as shipped | list `exports.yaml` under `fixed` | [`structure`](#structure) |
| Reject candidates by a rule of your own (a lint, a test, a forbidden word) | a checker function run after the built-in checks | [`check`](#check) |
| Give the learner a helper during training (a renderer, a validator) that the agent never sees | a tool declaration under `evolver_tools` | [`evolver_tools`](#evolver_tools) |
| Change how a learner should edit, not what it may edit | `.hermit/EVOLVER.md` | no metadata change |

Two rules hold throughout. When `structure` lists learnable paths, every file outside `.hermit/` must be learnable
or fixed, so adding a file means listing it. And `.hermit/` is the template's: edit it in the template root before
training starts, not in a trained adapter, where the check rejects the change. If the change is an edit to the
acting-side texts only, keep the template's `id` and `version`; if it changes `.hermit/adapter.yaml` or
`exports.yaml`, give the copy its own `template.id` (or a new `version`) so its tree is not mistaken for the
original's. Run `hermit check --config run.yaml` afterwards: it loads the template, the task and the Evolver and
reports what does not fit.

The sections below are the reference for each field.

A minimal `.hermit/adapter.yaml`:

```yaml
template:
  id: prompt-only
  version: "1.0"
structure:
  learnable:
    - exports.yaml
    - path: ADAPTER.md
      format: {text: true, max_bytes: 40000}
  fixed: [README.md]
```

The complete example from `docs/_examples/file_adapter/.hermit/adapter.yaml`:

```{literalinclude} ../_examples/file_adapter/.hermit/adapter.yaml
:language: yaml
```

## `template`

Identity of the template this adapter belongs to. Required.

| Key | Required | Rule |
|---|---|---|
| `id` | yes | lowercase slug: `[a-z][a-z0-9-]{0,63}`. Names the template in `run.yaml` (`template: prompt-only@1.0`), in the deployment's managed block and MCP server name, and in the tree |
| `version` | yes | nonempty string. With `id` it makes the `id@version` reference |
| `name` | no | display name; defaults to `id` |
| `contract` | no | the metadata contract this file follows; `"2"` today, for the `.hermit/` layout. Not validated, carried for forward compatibility |

## `structure`

The specification: which files a learner may change, in what form, and which must stay as the template ships
them. Optional: without it, every file outside `.hermit/` is learnable.

```yaml
structure:
  learnable:
    - exports.yaml                                 # a bare path: any content
    - ADAPTER.md
    - path: agents/*.md
      format: {text: true, max_bytes: 4000, frontmatter: [name, description]}
    - path: skills/**
      format: {text: true, max_bytes: 4000, max_files: 8}
  fixed:
    - tools.py
    - README.md
```

**`learnable`**: a list of entries, each a path or `{path, format}`. `path` is a glob relative to the adapter
root: `*` matches within one path segment, `**` across segments, and a directory name covers everything below it.
A file may be changed, added or deleted only if it matches a learnable path and no fixed path.

| Format key | Meaning |
|---|---|
| `text: true` | the file must decode as UTF-8 |
| `max_bytes: N` | size limit per file |
| `max_files: N` | at most N files may match this entry |
| `frontmatter: [key, …]` | the file starts with YAML frontmatter carrying these keys (implies text) |

**`fixed`**: paths (files or directories) that must exist exactly as in the template root; changing, deleting or
adding a file under one fails the check. `.hermit/` is always fixed and needs no entry.

When `structure` is present, every file outside `.hermit/` must be either fixed or covered by a learnable entry;
`exports.yaml` is the exception, learnable unless `fixed` lists it. A file outside both is a check failure, and the
Trainer removes it when it repairs a candidate. See [How does the specification limit
edits?](adapter.md#how-does-the-specification-limit-edits).

## `check`

```yaml
check: .hermit/check.py:check
```

Optional. A template's own checker, `file.py:function` with the path relative to the adapter root, loaded from the
**template root** (not the candidate) and run after the built-in checks: `check(adapter_path) -> list[dict]`, each
issue `{path, problem}`; a nonempty list rejects the candidate. It runs in a subprocess, 120 seconds, network isolated when bubblewrap is available. See
[Specification check](adapter.md#specification-check).

## `evolver_tools`

```yaml
evolver_tools:
  - name: render
    description: Render the context the way the host will see it.
    entrypoint: .hermit/preview.py:render
    input_schema: {type: object, additionalProperties: false}
    output_schema: {type: object}
```

Optional. Tools the **learner** gets during training, in the same form as a [tool declaration](#tool-declaration),
with the entrypoint resolved against the template root. They join `check_spec` (always present), the task's
`evolver_access.tools` and the run's `budget` / `llm` on the Evolver's tool endpoint; they are never exported to
the acting agent.

## `plugin-portable`

```yaml
plugin-portable: false
```

Optional, `true` or `false`; without it, `true`. Whether the template's adapters can leave hermit: `false` makes
`hermit deploy` (a host workspace) and `hermit convert` (a plugin) refuse them, with the reason, before writing
anything, so that an adapter that works only through hermit (training, evaluation, a `Session`) is never installed
without the part that needs hermit. A template whose adapters declare a [context tool](#per-task-context) must set
it to `false`: such an adapter does not load otherwise. The `reharness` template does.

## Exports

`exports.yaml` at the adapter root: what the acting agent receives, as a top-level mapping. Optional: an adapter
without it, or with an empty one, exports its context only. It is ordinary adapter content, learnable unless the
template's `structure` lists it under `fixed`, whether or not a learnable entry names it; check 3 of the specification
check requires it to parse, and repair restores it from the parent.

| Key | Value | Rule |
|---|---|---|
| `context` | `ADAPTER.md` | optional and fixed: the context is always the file `ADAPTER.md` at the adapter root; without it the context is empty. Naming any other file is an error |
| `tools` | list of [tool declarations](#tool-declaration) | names unique; each tool's code lives inside the adapter |
| `agents` | a directory, or `{entrypoint: file.py:function}` | roles (subagents). A directory holds one Markdown file per role, `<name>.md` with YAML frontmatter `name` and `description`, or `<name>/AGENT.md`, the layout of the Codex-tree templates, read as their harnesses read it (named by its directory, its `description` from its frontmatter, else its name); the body is its instructions, and a `README.md` there is not a role. An exporter function returns that list instead |
| `skills` | a directory | skill folders copied to the host as they are (`<directory>/<name>/SKILL.md`) |
| `hooks` | a JSON file | lifecycle hooks in Claude Code's hook schema, `{"hooks": {"<Event>": [{"matcher": ..., "hooks": [{"type": "command", "command": ...}]}]}}`, which Codex reads too; deployed with each command run from the adapter's copy. The file may be missing (a learner may add it); a deployment refuses one that does not parse |
| `reactions` | `{entrypoint: file.py:function}` | service integration only: a function the framework service calls on events; `deploy` ignores it |
| `concurrent_tool_calls` | boolean | service integration only: run the adapter's tool calls concurrently instead of one at a time |
| `context_tool` | the name of one declared tool | [per-task context](#per-task-context): the harness calls that tool once per task with `{query, task}` and appends the `text` it returns below `ADAPTER.md`; hidden from the agent's tools; the deploy check calls it once with an empty query; the template must declare [`plugin-portable: false`](#plugin-portable) |

All paths are relative to the adapter root and must stay inside it.

Only declared exports are connected to the host: an adapter without `exports.yaml` deploys as an instruction file
and a copy of its files under `hermit/<id>/`. How each export reaches Codex or Claude Code is in
[Acting interface](adapter.md#acting-interface). The example's `exports.yaml`:

```{literalinclude} ../_examples/file_adapter/exports.yaml
:language: yaml
```

### Tool declaration

```yaml
- name: count_words
  description: Count whitespace-separated words in text.
  entrypoint: tools.py:count_words
  input_schema:
    type: object
    properties: {text: {type: string}}
    required: [text]
    additionalProperties: false
  output_schema:
    type: object
    properties: {count: {type: integer}}
    required: [count]
  annotations: {readOnlyHint: true}
```

| Key | Required | Rule |
|---|---|---|
| `name` | yes | `[a-z][a-z0-9_]{0,63}`, unique within `exports.yaml`; the name the agent calls |
| `description` | yes | nonempty; shown to the agent as the tool's description |
| `entrypoint` | yes | `file.py:function`, a `.py` file inside the adapter; the function takes the input's keys as keyword arguments, may be `async`, and returns a dict |
| `input_schema` | yes | a JSON Schema object (`type: object`); arguments are validated against it before the call |
| `output_schema` | yes | a JSON Schema object; the result is validated against it |
| `annotations` | no | MCP tool annotations passed through to the host (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`) |

Schemas may use local `$ref`s only; remote and dynamic references are rejected. A tool function may declare a
reserved `_resources` parameter to receive framework bindings (the evidence store, deployment settings) from a
pipeline that supplies them; the agent cannot pass it.

### Per-task context

```yaml
# exports.yaml
tools:
  - name: reflections
    description: The top-k reflections of the bank for an instance's input.
    entrypoint: interface.py:reflections
    input_schema:
      type: object
      properties: {query: {type: string}, task: {type: object}}
      additionalProperties: false
    output_schema:
      type: object
      properties: {text: {type: string}}
      required: [text]
context_tool: reflections
```

`context_tool` names one of the declared tools as the harness's. The harness, not the agent, calls it once per
task, before the agent's first model call, with `query` (the task's input text) and `task` (its row), and appends
the `text` of the result below `ADAPTER.md` for that task. `Session.context(query=..., task=...)` makes the call
and keeps the whole result in `session.last_context`; the built-in `react` agent does both and records the result as
`RunOutput.extra["context_tool"]`. The tool is left out of `Session.tools`, `tool_specs` and the hosts' tool
servers, and `respond` refuses it: the agent cannot call it. A manifest that names an undeclared tool does not load.

The specification check's load check (check 5) calls the tool once with an empty query and an empty task, in a
run's sandbox (strict, without network or keys, when the sandbox is on; unconfined when `sandbox.enabled` is false), so the tool must answer that call without an endpoint (with no query there is
nothing to retrieve) and return an object with a `text` string. A host workspace or plugin has no per-task context,
so an adapter that names a context tool loads only when its template declares
[`plugin-portable: false`](#plugin-portable), and `deploy` and `convert` refuse it. The `reharness` template retrieves reflections this way
([ReHarness](../methods/reharness.md)).

## `serve` (design, not implemented)

`.hermit/adapter.yaml` does not accept a `serve` section. The implemented `hermit serve DIR` command starts a
per-session stdio MCP tool server, normally launched by a generated host entry. Generic standalone startup,
health checks, discovery, and relay lifecycle remain a [design proposal](../developer/service-design.md).
Do not add the proposed fields or flags to a working configuration.

## Migrating from Hermit 0.1

In Hermit 0.1 the metadata and the exports were one file, `adapter.yaml`, at the adapter root; Hermit now refuses
that layout and names it. To migrate an adapter or a template, in its directory:

```bash
mkdir .hermit
git mv adapter.yaml .hermit/adapter.yaml
git mv EVOLVER.md .hermit/EVOLVER.md        # each of these three only if present
git mv CARD.md .hermit/CARD.md
git mv check.py .hermit/check.py
```

Then edit `.hermit/adapter.yaml`: move the `exports` section into a new `exports.yaml` at the root (its contents
become the file's top level), delete the `origin` section, and point `check` to `.hermit/check.py`. A template that
lists `adapter.yaml`, `EVOLVER.md` or `check.py` under `fixed` drops those entries: `.hermit/` is always fixed. Every
adapter of a tree must carry the template's `.hermit/` byte for byte, so migrate the template and its adapters
together, and rebuild plugins made by `hermit convert` from the migrated adapter.

## What is not in the metadata or the exports

- **The content format of `ADAPTER.md`.** Beyond `structure`'s size and text rules, the context is free text; no
  frontmatter or sections are required or interpreted.
- **Dependencies.** A tool's imports must be importable where the adapter runs; `README.md` is the place to say
  what they are.
- **Learner guidance.** `.hermit/EVOLVER.md` tells the learner how to edit within `structure`; it is not exported.
- **Run settings.** Budgets, sandbox, data and the Evolver are in `run.yaml`; see [Configuration](configuration.md).
