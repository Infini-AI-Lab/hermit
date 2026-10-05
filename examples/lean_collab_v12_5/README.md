# Example: the Lean collaboration adapter v12.5

A finished adapter for five Codex agents proving one Lean theorem together (formal-conjectures problems). It is a node
of the `lean-collab@1.0` tree: the template's fixed team service plus the learned skills of v12.5. See
[`adapter/CARD.md`](adapter/CARD.md) for how it was obtained and how it performs.

```text
adapter/
  adapter.yaml        template, exports (context, five team tools, reactions, skills), structure, origin
  ADAPTER.md          context every agent reads (learnable)
  skills/             good-map, good-leaf, kernel-check, lean-workflow, solving-together (learnable)
  collab.py, assembly.py, reactions.py   the team service: check, pull, refute, say, plan (fixed)
  EVOLVER.md, README.md, CARD.md
```

Check and deploy it:

```bash
python -m harnessadapter.spec examples/lean_collab_v12_5/adapter --root harnessadapter/templates/lean_collab
harnessadapter deploy --adapter examples/lean_collab_v12_5/adapter --output /tmp/agent-A1 --host codex
```

Running it on problems needs the multi-agent launcher (one team service per run, five sandboxed Codex agents, the
Lean grader), which lives in `lean-results/fc_multi/run_collab.py` and is not yet wrapped as a pipeline of this
framework. The tools expect `COLLAB_DIR`, `COLLAB_AGENT`, `COLLAB_WORKSPACE`, `COLLAB_TARGET`, `COLLAB_REPO` and
`COLLAB_LAKE` per agent (see `adapter/README.md`).
