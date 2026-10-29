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
| `contract` | no | the metadata contract this file follows; `"1"` today. Not validated, carried for forward compatibility |

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

When `structure` is present, every file outside `.hermit/` must be either fixed or covered by a learnable entry. A file
outside both is a check failure, and the Trainer removes it when it repairs a candidate. See
[How does the specification limit edits?](adapter.md#how-does-the-specification-limit-edits).

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

## Exports

`exports.yaml` at the adapter root: what the acting agent receives, as a top-level mapping. Optional: an adapter
without it, or with an empty one, exports its context only. It is ordinary adapter content, learnable unless the
template's `structure` fixes it; check 3 of the specification check requires it to parse, and repair restores it
from the parent.

| Key | Value | Rule |
|---|---|---|
| `context` | `ADAPTER.md` | optional and fixed: the context is always the file `ADAPTER.md` at the adapter root; without it the context is empty. Naming any other file is an error |
| `tools` | list of [tool declarations](#tool-declaration) | names unique; each tool's code lives inside the adapter |
| `agents` | a directory, or `{entrypoint: file.py:function}` | roles (subagents). A directory holds one Markdown file per role, YAML frontmatter with `name` and `description`, the body its instructions. An exporter function returns that list instead |
| `skills` | a directory | skill folders copied to the host as they are (`skills/<name>/SKILL.md`) |
| `reactions` | `{entrypoint: file.py:function}` | service integration only: a function the framework service calls on events; `deploy` ignores it |
| `concurrent_tool_calls` | boolean | service integration only: run the adapter's tool calls concurrently instead of one at a time |

All paths are relative to the adapter root and must stay inside it.

Only declared exports produce anything on the acting side: an adapter without `exports.yaml` deploys as an
instruction file and nothing else. How each export reaches Codex or Claude Code is in
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

## `serve` (design, not implemented)

`.hermit/adapter.yaml` does not accept a `serve` section. The implemented `hermit serve DIR` command starts a
per-session stdio MCP tool server, normally launched by a generated host entry. Generic standalone startup,
health checks, discovery, and relay lifecycle remain a [design proposal](../developer/service-design.md).
Do not add the proposed fields or flags to a working configuration.

## What is not in the metadata or the exports

- **The content format of `ADAPTER.md`.** Beyond `structure`'s size and text rules, the context is free text; no
  frontmatter or sections are required or interpreted.
- **Dependencies.** A tool's imports must be importable where the adapter runs; `README.md` is the place to say
  what they are.
- **Learner guidance.** `.hermit/EVOLVER.md` tells the learner how to edit within `structure`; it is not exported.
- **Run settings.** Budgets, sandbox, data and the Evolver are in `run.yaml`; see [Configuration](configuration.md).
