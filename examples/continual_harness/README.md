# Continual Harness (Pokemon Red)

[Continual Harness](https://github.com/sethkarten/continual-harness): an LLM agent plays Pokemon Red through a
PyBoy emulator. This example wraps it as a Hermit task so Hermit drives the agent and **evolves the harness** —
here the harness is the playing policy in `ADAPTER.md` — while the Continual Harness repo supplies the environment
(the emulator, the game server, and the milestone scoring).

- `utils/agent.py:run` starts the Continual Harness game server (in its own venv, headless) and plays one *window*
  of `task["steps"]` button presses with the adapter's policy. Each step it reads the server's LLM-formatted state
  (an ASCII map with a walkability legend, a movement preview, and the screenshot), asks the model for one button,
  and presses it. It returns a compact per-step trace and a summary so the Evolver can see how the policy behaved.
- `utils/judge.py:score` scores the window by how far along the game the policy got: the furthest milestone reached
  on Red's ordered path (bedroom → Pallet Town → Oak's Lab → … → Champion), scaled to [0, 1], plus a small bounded
  exploration term (distinct map tiles visited) so short windows still give the Evolver a gradient.
- `GLM 5.3 Flash` (Fireworks) acts; the GEPA Evolver rewrites `ADAPTER.md`. The game server runs in the Continual
  Harness checkout's own venv and is reached over HTTP, so the sandbox is off.

## Setup

Install the framework with the `gepa` extra ([Installation](../../docs/get-started/installation.md)). Then set up a
Continual Harness checkout (it is not vendored here):

1. `git clone https://github.com/sethkarten/continual-harness` and create its own virtualenv with PyBoy
   (`opencv-python-headless`, not `opencv-python`, so it runs headless).
2. Build the Pokemon Red ROM from source — the [pret/pokered](https://github.com/pret/pokered) disassembly with the
   RGBDS toolchain — and place the result at `PokemonRed-GBC/pokered.gbc` in the checkout. (Build from source; no ROM
   is distributed here.)
3. In `pokemon_env/__init__.py`, import the Emerald memory reader lazily (wrap `from .memory_reader import
   PokemonEmeraldReader` in `try/except ImportError`) so the Red/PyBoy path loads without the mGBA native library.
   (Worth upstreaming.)

Then point this example at the checkout and the model:

```bash
export HERMIT_CH_DIR=/path/to/continual-harness          # the checkout (its game server lives here)
export HERMIT_CH_PYTHON=$HERMIT_CH_DIR/.venv/bin/python   # that checkout's venv interpreter (this default is assumed)
export FIREWORKS_API_KEY=...                              # GLM 5.3 Flash acts and (by default) reflects
```

## Run

```bash
hermit eval  --config examples/continual_harness/prompt_only/run.yaml --adapter root    # the seed policy
hermit train --config examples/continual_harness/prompt_only/run.yaml                    # GEPA evolves ADAPTER.md
hermit eval  --config examples/continual_harness/prompt_only/run.yaml --adapter final    # the evolved policy
```

`prompt_only/smoke.yaml` is a tiny version (short windows, one training step) to check the wiring first. An adapter's
score is `mean_score` in `prompt_only/runs/<run name>/eval/<selector>/summary.json`.
