# Lean collaboration

The `lean-collab@1.0` template equips acting agents to work on Lean proofs together. It exports instructions, five
skills, and tools for compiling proofs, sharing verified helpers, tracking open subgoals, and communicating.

The template and host deployment are available in this branch. The multi-agent launcher and grading pipeline have
not been migrated into an end-to-end training recipe. Use [OfficeQA](officeqa.md) for a complete training example.

## What the adapter contains

```text
harnessadapter/templates/lean_collab/
  adapter.yaml
  ADAPTER.md       learnable instructions
  skills/          learnable skills
  collab.py        fixed collaboration tools
  assembly.py      fixed proof assembly code
  reactions.py    fixed reaction handler
  EVOLVER.md
  README.md
```

Its tools include `check`, `pull`, `refute`, `say`, and `plan`. These declarations are in `adapter.yaml`.
Shared runtime data such as verified lemmas and the team channel lives under `COLLAB_DIR`, outside the adapter.

## Prepare a workspace

After [installation](../get-started/installation.md), run from the repository root:

```bash
harnessadapter deploy \
  --adapter harnessadapter/templates/lean_collab \
  --output .demo/lean-workspace \
  --host codex
```

This generates the host workspace. It does not start a team, install Mathlib, or provide a proof task. The tool
processes need a working Lean/Mathlib setup and the environment variables below:

| Variable | Purpose |
|---|---|
| `COLLAB_DIR` | Shared collaboration state for this run. |
| `COLLAB_AGENT` | Identity of the calling agent. |
| `COLLAB_WORKSPACE` | That agent's working directory. |
| `COLLAB_TARGET` | Target theorem name. |
| `COLLAB_REPO` | Lean project used by the tools. |
| `COLLAB_LAKE` | Path to the Lake executable. |

The launcher must also arrange the Lean toolchain environment (`ELAN_HOME`, `PATH`, and `HOME` as needed). See
`harnessadapter/templates/lean_collab/README.md` for the current tool behavior. Each agent needs its own identity
and workspace while sharing the intended run's collaboration directory.

## What is needed to train it

Provide a pipeline that launches the agents with the adapter and collects a proof, a judge that checks the result,
and task data and splits. The framework calls those through the same [task interface](../framework/task.md) used by
OfficeQA. Select `lean-collab@1.0` as the template and a compatible Evolver. Its permitted edits cover instructions
and skills; the collaboration implementation remains fixed.
