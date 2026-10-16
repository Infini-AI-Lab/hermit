# Lean swarm

Five Codex agents prove one Lean theorem together. The adapter gives them a shared plan, a lemma library and a
channel through five tools (`check`, `pull`, `refute`, `say`, `plan`), plus the instructions and skills that tell
them how to split a proof and work on its parts; the first complete proof wins. The problems are open statements
from [formal-conjectures](https://github.com/google-deepmind/formal-conjectures) with a known proof, binned by the
size of that proof. This example holds the finished adapter, v12.5 of the `lean-collab@1.0` template (the fixed
team service plus the skills learned over the research rounds), and compares it with single agents and with a
team that shares only free-text notes.

## Setup

```bash
pip install -e .                                                        # this framework, Python 3.12+
python -m harnessadapter.spec examples/lean_swarm/adapter --root harnessadapter/algorithms/templates/lean_collab
```

Also needed: the Codex CLI on PATH, bubblewrap, a Lean toolchain with a Mathlib checkout the tools can compile
against (`COLLAB_REPO`, `COLLAB_LAKE`; nothing here installs it), and a Fireworks key in `FIREWORKS_API_KEY`.

## Run

One team service per run, then one deployed workspace per agent:

```bash
python -m harnessadapter.runtime.service --config run.json                                       # the team service
harnessadapter deploy --adapter examples/lean_swarm/adapter --workspace /work/A1 --host codex      # per agent A1..A5
```

`run.json` names the adapter, a control socket, one socket per agent and the `COLLAB_*` variables
(see [docs/recipes/lean-collaboration.md](../../docs/recipes/lean-collaboration.md) for a full one). The launcher
that does this for a batch of problems, sandboxes the agents, carries the service's interrupt and resume commands
and grades the proof lives outside this repository (`lean-results/fc_multi/run_collab.py`); it is not yet a
pipeline of this framework, so there is no `harnessadapter train` for this task.

## Results

54 formal-conjectures problems whose known proofs are 800 to 3,000 lines long, five DeepSeek V4.1 Flash agents
(reasoning effort high) per problem in every method, one proof attempt per problem with a two-hour budget; a problem counts as
solved when a proof compiles without `sorry` or extra axioms.

![problems solved against time](solved_vs_time.png)

Problems solved against time, normalised to an API speed of 75 output tokens/s. avg@5 and best@5 are five
independent single agents; the shared-notes team shares one notes file.

## Code

```text
adapter/
  adapter.yaml        template, exports (context, five team tools, reactions, skills), structure, origin
  ADAPTER.md          context every agent reads (learnable)
  skills/             good-map, good-leaf, kernel-check, lean-workflow, solving-together (learnable)
  collab.py           the five tools: check, pull, refute, say, plan (fixed)
  assembly.py         assembling the proof from the verified parts (fixed)
  reactions.py        the service's interrupt and resume decisions (fixed)
  CARD.md             how the adapter was obtained and evaluated; EVOLVER.md, README.md
```

The tools read `COLLAB_DIR`, `COLLAB_AGENT`, `COLLAB_WORKSPACE`, `COLLAB_TARGET`, `COLLAB_REPO` and `COLLAB_LAKE`
from the environment the service sets per agent (see `adapter/README.md`).
