# Adapter Template and Adapter

:::{note}
Agreed 2026-10-05; implemented in 0.2.0. Implementation notes at the end of this page.
:::

An **adapter template** and an **adapter** are different things. The template is a structure and its specification;
an adapter is a concrete, deployable artifact built on that structure.

## The tree

Templates and adapters form a tree:

```text
template (root)
 ├── adapter A1      evolved with Evolver P on task X
 │    ├── A1.1       continued from A1 on task X
 │    └── A1.2       started from A1, evolved on task Y
 ├── adapter B1      evolved with Evolver Q on task X
 └── adapter C1      evolved with Evolver R on task Z
```

| Tree element | Meaning |
|---|---|
| Root | The template. It fixes the structure of every node in its tree: what is learnable and what is fixed. |
| Node | An adapter: every checkpoint and every final result. Each node is complete, downloadable and deployable. |
| Edge | One training run from a parent node: the Evolver, the evolving task, the config and the run records. |

Any node can be the starting point of a new training run (`start_from`, see [Configuration](configuration.md)).
Templates do not derive from one another: each template is the root of its own tree, and a new structure is a new
template.

### The tree is a git repository

One repository holds one tree. Only the Trainer runs git; the Evolver edits a working copy and reads earlier versions
through a read-only history view.

| Tree element | In git |
|---|---|
| Root (template) | The first commit |
| Node (adapter) | A commit. Downloading an adapter is cloning the repository and checking out that commit. |
| Node id, parent | The commit hash, the commit's parent |
| One step of a run | A commit whose message records the Evolver, the task, the epoch and step, the summary, and where the run records are |
| One training run | A branch named after the run, for example `lean-gepa-01` |
| Checkpoint, final result | A tag prefixed with the run name: `lean-gepa-01/ckpt-epoch-2`, `lean-gepa-01/ckpt-step-15`, `lean-gepa-01/final` |

When an adapter handed back by the Evolver fails the binding check, the Trainer commits it exactly as handed back, then
commits the smallest repair that makes it conform (see [Trainer](trainer.md#when-the-binding-check-fails)). Both commits
stay on the run's branch, so nothing is lost. The unrepaired commit is marked invalid in its message; it is not an
adapter, is never tagged, and cannot be used as `start_from`. Only valid commits are nodes of the tree.

## Adapter template

A template is an initial adapter together with a specification. It is written by the template provider and contains
no task content and no learning method.

```text
my_template/
  adapter.yaml   manifest: identity, exports, structure (the specification), check
  ADAPTER.md     context for the acting agent (learnable or fixed, as the specification says)
  EVOLVER.md     guide for the Evolver: the structure only
  check.py       template-specific checks (optional)
  README.md      for people: what it is for, which hosts it supports, how to deploy it
  ...            fixed code and files; initial content of the learnable parts
```

| Content | Required | Purpose |
|---|---|---|
| `adapter.yaml` → `template` | yes | Template id, version, contract version |
| `adapter.yaml` → `exports` | yes | What the adapter becomes in the acting harness after deployment: context, MCP tools, agent roles, reactions, resources it needs |
| `adapter.yaml` → `structure` | yes | The **specification**. Learnable parts: the paths that may change and the format each must follow (for example `memory/*.md` with a scope field, `tools/*/` with a `test.py`). Fixed parts: the paths that must not change. |
| `adapter.yaml` → `check` | no | Entry point of the template's own check function, for example `check: check.py:check`. Without it the framework default is used. |
| Fixed files | as needed | Code and files shared by every adapter in the tree and never learned (a dispatcher, a context renderer, a collaboration service) |
| Initial learnable content | yes | The learnable content of the root; usually empty, may hold defaults |
| `EVOLVER.md` | yes | What each learnable part is, its format, what a valid change looks like. No learning method and no task content. |
| `README.md` | yes | For people |

The specification is binding: the Trainer repairs any adapter that changes a fixed part or breaks a declared format.
A template provider who wants a piece of code to evolve declares it as a learnable part and states its format and the
checks it must pass.

```yaml
# adapter.yaml (template sections)
template:
  id: lean-collab
  version: "1.0"
  contract: "1"
exports:
  context: ADAPTER.md
  tools: collab.py
structure:
  learnable:
    - path: ADAPTER.md
    - path: skills/*.md
      format: {max_bytes: 20000}
  fixed:
    - collab.py
    - adapter.yaml
check: check.py:check
```

## Adapter

An adapter is self-contained: a complete copy of its template's fixed parts plus learned content, so it can be
downloaded and deployed directly.

| Content | Written by |
|---|---|
| All fixed parts of the template, unchanged (including the `template`, `exports` and `structure` sections of `adapter.yaml`) | Trainer (copied) |
| Current content of the learnable parts | Evolver |
| `adapter.yaml` → `origin`: the root template's id and version | Trainer |
| `CARD.md` | Trainer (generated); the user may add to it |

The adapter's id, parent and history are its git commit, the commit's parent and the git log; they are not repeated in
files. Whether the fixed parts are unchanged is checked with git: the diff between the root commit and the adapter,
restricted to the fixed paths, must be empty.

`CARD.md` is the adapter's card, like a model card, written for whoever downloads it: the template and parent; how the
adapter was evolved (Evolver, task, model, epochs and steps); its `eval` results and cost; a summary of what was
learned; where it has and has not been tested; and the run's sandbox, network and data-exposure settings with any
warnings they raised. It does not affect how the adapter runs.

### What is not part of an adapter

| Item | Where it lives |
|---|---|
| The Evolver's Training State (for example a GEPA candidate pool, an MWM journal) | The run directory |
| Data produced while the adapter runs (for example a shared lemma library) | The run directory of each rollout or `eval` |
| Trajectories and scores as run records | The run directory; commit messages point to them |
| Task content (task-info, data) | The evolving task |

Trajectories may still be part of an adapter as **learned content**. That is between the Evolver and the template:
if the template's `structure` declares a learnable data path (for example `evidence/`), the Evolver may copy any
material it can read into it, up to every complete trajectory, so the acting agent can read them at deploy time (as
MWM does). The Trainer treats this as ordinary learnable content.

## `check_spec`

`check_spec` answers one question: does this adapter conform to its template's specification and deploy? It does not
judge quality, run tasks, call models or check task rules.

| # | Check | Rule from | Code by |
|---|---|---|---|
| 1 | Fixed parts unchanged (git diff against the root on the fixed paths is empty) | `structure.fixed` | framework library |
| 2 | Every added or changed file lies in a learnable path | `structure.learnable` | framework library |
| 3 | `adapter.yaml` parses; its `template`, `exports` and `structure` sections equal the root's | manifest | framework library |
| 4 | Learnable parts follow their declared formats (fields, names, size limits) | `structure` | framework library |
| 5 | Deploys: context renders, tool entry points import, roles convert (no acting agent started) | `exports` | framework library |
| 6 | Template-specific requirements a declaration cannot express (for example: every new tool has a passing test) | template provider | template provider |

A template that declares no check function gets the framework default, which runs checks 1–5; a template that makes
everything learnable and declares no formats is then only checked to load and deploy. The function receives the
adapter's path, runs without network and with a time limit, and returns `CheckResult(passed, issues)`, each issue
naming the path, the check and the problem. Because it travels with the template, a downloaded adapter can be checked
on its own.

| Caller | When | Purpose |
|---|---|---|
| `harnessadapter check` | Before training, on the starting adapter | The template itself must conform |
| Evolver | Any time during a call (optional) | Feedback, so it can fix problems itself |
| Trainer | After every call (binding) | Nothing that fails flows into the next step |

## Implementation notes

- `adapter.yaml` sections: `template`, `exports`, `structure`, `check`, `evolver_tools` (tools the template offers
  to Evolvers, declared like adapter tools; their functions may take `_adapter` and `_root`), and `origin` (written
  by the Trainer on every committed adapter).
- `exports` keys: `context`, `agents`, `tools`, `reactions`, `concurrent_tool_calls`, `skills` (a directory of skill
  folders, each with `SKILL.md`).
- Format keys of a learnable path: `text` (UTF-8), `max_bytes` (per file), `max_files` (per pattern), `frontmatter`
  (required YAML frontmatter keys). Patterns are globs; `**` crosses directories; a directory pattern covers its files.
- `CARD.md` and `adapter.yaml` are framework files: neither learnable nor fixed; check 3 compares the manifest's fixed
  sections, so `origin` may differ from the root's.
- A template check function returns a list of `{path, problem}` or `{passed, issues}`; it runs in a subprocess,
  without network when bubblewrap is available, with a 120-second limit.
- Command line: `python -m harnessadapter.spec ADAPTER --root TEMPLATE`.
