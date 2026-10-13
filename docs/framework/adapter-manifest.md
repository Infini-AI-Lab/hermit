# adapter.yaml reference

`adapter.yaml` is the manifest at the root of every adapter directory. It names the template the adapter comes
from, declares what the adapter exports to an acting agent, and states which files a learner may change. The
rules below are the ones `harnessadapter.adapter.manifest` and `harnessadapter.adapter.loader` enforce; a
manifest that breaks one does not load, and a candidate whose manifest drifts from its template fails the
specification check.

The file has six sections and no others:

| Section | Who writes it | Who reads it |
|---|---|---|
| [`template`](#template) | the template author | everyone: identity and compatibility |
| [`exports`](#exports) | the template author | the acting side: hosts, `deploy`, custom pipelines |
| [`structure`](#structure) | the template author | the Trainer's specification check; the learner, through `EVOLVER.md` |
| [`check`](#check) | the template author | the specification check |
| [`evolver_tools`](#evolver_tools) | the template author | the Evolver's tool endpoint |
| [`origin`](#origin) | the Trainer | people and the tree |

A seventh, [`serve`](#serve-design-not-implemented), is proposed and not yet read by anything.

The first five are **fixed across a tree**: every adapter committed under a template carries them byte-for-byte as
the template's root does (check 3 of the specification check). An acting integration needs only `template` and
`exports`; `flatten()` returns exactly those, with `context` filled in.

A minimal manifest:

```yaml
template:
  id: prompt-only
  version: "1.0"
exports: {}
structure:
  learnable:
    - path: ADAPTER.md
      format: {text: true, max_bytes: 40000}
  fixed: [EVOLVER.md, README.md]
```

The complete example from `docs/_examples/file_adapter/adapter.yaml`:

```{literalinclude} ../_examples/file_adapter/adapter.yaml
:language: yaml
```

## `template`

Identity of the template this adapter belongs to. Required.

| Key | Required | Rule |
|---|---|---|
| `id` | yes | lowercase slug: `[a-z][a-z0-9-]{0,63}`. Names the template in `run.yaml` (`template: prompt-only@1.0`), in the deployment's managed block and MCP server name, and in the tree |
| `version` | yes | nonempty string. With `id` it makes the `id@version` reference |
| `name` | no | display name; defaults to `id` |
| `contract` | no | the manifest contract this file follows; `"1"` today. Not validated, carried for forward compatibility |

## `exports`

What the acting agent receives. Required, may be empty (`{}`): then the adapter exports its context only.

| Key | Value | Rule |
|---|---|---|
| `context` | `ADAPTER.md` | optional and fixed: the context is always the file `ADAPTER.md` at the adapter root, which must exist. Naming any other file is an error |
| `tools` | list of [tool declarations](#tool-declaration) | names unique; each tool's code lives inside the adapter |
| `agents` | a directory, or `{entrypoint: file.py:function}` | roles (subagents). A directory holds one Markdown file per role, YAML frontmatter with `name` and `description`, the body its instructions. An exporter function returns that list instead |
| `skills` | a directory | skill folders copied to the host as they are (`skills/<name>/SKILL.md`) |
| `reactions` | `{entrypoint: file.py:function}` | service integration only: a function the framework service calls on events; `deploy` ignores it |
| `concurrent_tool_calls` | boolean | service integration only: run the adapter's tool calls concurrently instead of one at a time |

All paths are relative to the adapter root and must stay inside it.

Only declared exports produce anything on the acting side: a manifest with `exports: {}` deploys as an instruction
file and nothing else. How each export reaches Codex or Claude Code is in [Acting interface](adapter.md#acting-interface).

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
| `name` | yes | `[a-z][a-z0-9_]{0,63}`, unique within the manifest; the name the agent calls |
| `description` | yes | nonempty; shown to the agent as the tool's description |
| `entrypoint` | yes | `file.py:function`, a `.py` file inside the adapter; the function takes the input's keys as keyword arguments, may be `async`, and returns a dict |
| `input_schema` | yes | a JSON Schema object (`type: object`); arguments are validated against it before the call |
| `output_schema` | yes | a JSON Schema object; the result is validated against it |
| `annotations` | no | MCP tool annotations passed through to the host (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`) |

Schemas may use local `$ref`s only; remote and dynamic references are rejected. A tool function may declare a
reserved `_resources` parameter to receive framework bindings (the evidence store, deployment settings) from a
pipeline that supplies them; the agent cannot pass it.

## `structure`

The specification: which files a learner may change, in what form, and which must stay as the template ships
them. Required.

```yaml
structure:
  learnable:
    - ADAPTER.md                                   # a bare path: any content
    - path: agents/*.md
      format: {text: true, max_bytes: 4000, frontmatter: [name, description]}
    - path: skills/**
      format: {text: true, max_bytes: 4000, max_files: 8}
  fixed:
    - tools.py
    - EVOLVER.md
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
adding a file under one fails the check. `adapter.yaml` and `CARD.md` are always framework-owned and need no
entry.

Every file in an adapter must be either fixed or covered by a learnable entry. A file outside both is a check
failure, and the Trainer removes it when it repairs a candidate. See
[How does the specification limit edits?](adapter.md#how-does-the-specification-limit-edits).

## `check`

```yaml
check: check.py:check
```

Optional. A template's own checker, `file.py:function` loaded from the **template root** (not the candidate), run
after the built-in checks: `check(adapter_path) -> list[dict]`, each issue `{path, problem}`; a nonempty list
rejects the candidate. It runs in a subprocess, 120 seconds, network isolated when bubblewrap is available. See
[Specification check](adapter.md#specification-check).

## `evolver_tools`

```yaml
evolver_tools:
  - name: render
    description: Render the context the way the host will see it.
    entrypoint: preview.py:render
    input_schema: {type: object, additionalProperties: false}
    output_schema: {type: object}
```

Optional. Tools the **learner** gets during training, in the same form as a [tool declaration](#tool-declaration),
with the entrypoint resolved against the template root. They join `check_spec` (always present), the task's
`evolver_access.tools` and the run's `budget` / `llm` on the Evolver's tool endpoint; they are never exported to
the acting agent.

## `origin`

```yaml
origin:
  template: file-example
  version: "1.0"
```

Written by the Trainer into every adapter it commits: the template id and version the tree started from. Not
part of the fixed sections; a hand-written adapter has none.

## `serve` (design, not implemented)

The consumption-end assumption the framework is moving to: **the manifest states which MCP tools the adapter
provides, and the adapter can start a healthy MCP server that provides them.** A host then needs only two things
from the adapter: how to start that server and how to tell it is up. Today the first is decided by `deploy` /
`convert` when they write the host's configuration, and the second is not checked at all. The proposed section:

```yaml
serve:
  transport: stdio          # stdio: the host starts one server per session; http: a standalone service
  command: harnessadapter serve .
  health: tools/list        # up when it answers initialize and list_tools reports exactly exports.tools
  lifecycle: session        # session: the host starts and stops it; standalone: the adapter's launcher does
```

| Key | Meaning |
|---|---|
| `transport` | `stdio` for stateless tools (the per-session wrapper, today `harnessadapter.runtime.mcp_server`); `http` for an adapter that needs state shared across sessions or agents, or reactions (today `harnessadapter.runtime.service`) |
| `command` | the one way to start the server; fixed, so a host configuration only points at the adapter directory |
| `health` | the readiness rule: the MCP handshake succeeds and `list_tools` matches `exports.tools` |
| `lifecycle` | `session`: the host starts and stops the server with its session. `standalone`: the process the host starts is only a stdio relay; it starts the shared service if none is running (a locked singleton) or attaches to it, and the service stops itself after an idle period once the last client is gone. Either way the user runs nothing extra |

### How the standalone service starts

Nobody starts it by hand. The host runs the configured line, `harnessadapter serve DIR`, at the start of every
session, believing it starts an ordinary stdio MCP server. What that process does when the manifest says
`lifecycle: standalone`:

```
session 1 starts  →  host runs: harnessadapter serve DIR
   1. read the manifest: lifecycle = standalone
   2. take the file lock DIR/.serve.lock; look for a live endpoint in DIR/.serve/endpoint (socket, pid)
   3. none → fork a detached daemon (own session) that listens on a unix socket and writes the endpoint back;
      one found → skip
   4. release the lock; become a stdio ↔ service relay; register with the service as client N
      (this is the per-agent identity the service needs, one socket per agent)
sessions 2…n     →  the same command; step 3 finds the service and connects
last client gone →  the service waits an idle period, then exits and removes the endpoint
```

So the host presses the start button, the adapter owns the lifecycle, and the user sees only `deploy` or
`convert` followed by starting the agent. This covers one machine. Two cases stay exceptions, handled by the
same command's explicit form rather than by a new entry point:

| Case | How |
|---|---|
| the service must run on another machine, or be warm before any agent | someone runs `harnessadapter serve DIR --standalone` there; `deploy` / `convert` are given `--serve-url`, and the host configuration then holds a URL instead of a local command |
| reactions that interrupt or resume an agent | the service can decide them, but only a host with a control channel can deliver them; today only the Lean collaboration run script plays that role |

`serve` is never typed by a user: it is the line `deploy` and `convert` write into the host's configuration, and
the host starts it. The user-facing consumption commands stay two, `deploy` (into a project) and `convert` (into
the agent). `serve` is host-agnostic by construction: it speaks MCP and nothing else, so the same process serves Codex,
Claude Code, Pi, Hermes or a custom pipeline. Everything host-specific (where the command line goes, which plugin
slot takes the context) stays in `deploy` and `convert`. With this section, `deploy` and `convert` reduce to
copying context, skills and roles and writing the `command` line into the host's own format; `harnessadapter check` can verify `health` before a run, and `serve` self-checks
at start. Nothing in this section exists yet: the `serve` command, the `health` check and the `lifecycle` switch
are design; `exports.tools` and the two runtimes are what exists.

## What is not in the manifest

- **The content format of `ADAPTER.md`.** Beyond `structure`'s size and text rules, the context is free text; no
  frontmatter or sections are required or interpreted.
- **Dependencies.** A tool's imports must be importable where the adapter runs; `README.md` is the place to say
  what they are.
- **Learner guidance.** `EVOLVER.md` tells the learner how to edit within `structure`; it is not exported.
- **Run settings.** Budgets, sandbox, data and the Evolver are in `run.yaml`; see [Configuration](configuration.md).
