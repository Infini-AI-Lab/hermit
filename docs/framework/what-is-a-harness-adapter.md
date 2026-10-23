# What is a harness adapter?

A **harness adapter** is a directory of instructions, skills, roles, or tools that an existing agent consumes.
It also carries a training specification: which files a learner may edit, which must remain fixed, and how a
candidate is checked. Training changes the directory rather than the agent's model weights.

You can write an adapter manually, train it from task feedback, or start from an included adapter.

## The consumption end: a plugin definition, plus an optional service

A minimal context adapter contains `ADAPTER.md`. More capable adapters export skills, roles, and callable tools:

```text
adapter/
  adapter.yaml      template identity, exports, learnable/fixed rules
  ADAPTER.md        instructions consumed by the acting agent
  EVOLVER.md        editing guidance consumed by the learner
  skills/          optional reusable procedures
  agents/          optional role definitions
  tools.py         optional declared tool implementations
```

The acting pipeline decides how these exports reach its agent. A custom model loop can read the context and call
declared tools through `Adapter`. A supported host can receive generated workspace files or a converted plugin.
A tool can depend on a separate service, as in [Lean collaboration](../recipes/lean-collaboration.md); the generic
manifest-driven service lifecycle remains a [proposal](../developer/service-design.md).

### Two ways to use an adapter

| Route | Output | Use when |
|---|---|---|
| `deploy` | A Codex, Claude Code, or Pi workspace | You want project instructions, skills, roles, and tool configuration |
| `convert` | A native plugin package for a supported target | You want host-native packaging and can accept that target's export limits |
| Custom pipeline | Your agent receives the exports directly | You own the acting loop or need dynamic integration |

```bash
hermit deploy --adapter DIR --workspace WS --host codex
hermit convert --adapter DIR --output OUT --target claude-code
```

Both commands exist in this checkout. They generate files; they do not launch an acting agent or prove that the
adapter improves it. Plugin conversion can refuse unsupported exports. See the
[host capability matrix](commands.md#what-each-hosts-plugin-carries) before choosing a target.

Workspace tool configuration refers to local source paths. Plugin conversion embeds an adapter copy. Tool
consumers need the framework and the adapter's Python dependencies wherever tools execute.

## The training end: built-in training support

The template declares the editable scope in `adapter.yaml`. The learner reads `EVOLVER.md` and task feedback,
then edits a working adapter. The Trainer checks those files against the template, records the resulting version,
and supplies the accepted adapter to the next rollout batch.

| Training element | Purpose |
|---|---|
| `structure.learnable` | Files the learner may change, with optional format and size limits |
| `structure.fixed` | Files that must stay as the template shipped them |
| `check` | Optional template-owned checker in addition to built-in file checks |
| `evolver_tools` | Helpers available to the learner, not acting-agent exports |
| Adapter tree | Git history, run branches, checkpoint and final tags |
| `CARD.md` | Framework-written provenance and recorded evaluation |

A method may use validation to select candidates, but structural acceptance by the Trainer is not itself a
performance gate. Evaluate the selected adapter on held-out tasks.

## Where this stands

The framework supports the context/skills/roles/tools contract, training/versioning, workspace deployment,
native plugin conversion, and a per-session stdio MCP tool server. It does not implement the proposed manifest
`serve` section, generic standalone service launch/relay lifecycle, or dynamic context delivery in ordinary host
plugins. See [Compatibility](../reference/compatibility.md).

Continue with [Quick Start](../get-started/quickstart.md) for an executable learning cycle,
[Using and training adapters](adapter-lifecycle.md) for concrete handoffs, or
[adapter.yaml](adapter-manifest.md) for the manifest contract.
