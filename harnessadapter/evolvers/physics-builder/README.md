# Physics Builder

A diagnostic and capability-building Evolver for physics-related animation tasks. It uses the MWM architecture and lifecycle, but replaces the learning objective with: identify consequential failures, investigate causes, and deliver useful knowledge, computational tools, checkers or reusable implementation components.

Animation Coach remains a separate preset. This version does not inherit its contract or preseed cloth/ocean solutions, and does not require validation data or mandatory tool creation.

## Run

```sh
harnessadapter evolve --architecture mwm --evolver physics-builder \
  --learning-material /path/to/learning-material \
  --model gpt-5.6-sol --reasoning-effort high \
  --no-network --timeout 1200 \
  --output /path/outside-framework/evolution
```

Use a learned adapter path instead of `--architecture mwm` to continue. Add `--prepare-only` to inspect the session without calling a model. Network access follows framework configuration; the command above disables learner command networking. Model and effort are run settings, not hardcoded requirements.

## Method files

- PROMPT.md: the complete core instruction shown to the learner.
- materials/CONTRACT.md: diagnosis, capability selection, validation and delivery details.
- METHOD.md: actual runtime capabilities and limitations.
- evolver.toml: backend defaults and shared MWM maintenance.

The learner also sees the adapter guide, LearningMaterial README, framework policy, and current MWM status. PROMPT.md is the core method prompt, not a claim that these additional inputs disappear.

## Current limits

This preset does not implement a physics engine or fix executable-tool portability. Scratch diagnostics can use the granted run_command capability. MWM callable admission still requires Linux bubblewrap; it is unavailable on this Mac. Successful scratch execution does not admit a callable. Retrievable source artifacts remain possible, but must be distinguished from admitted tools and integrated/tested by the acting agent. No browser is supplied implicitly.

Use a session output directory outside the editable framework checkout: the default .demo location can overlap a runtime-readable root and fail the existing preflight. Old runs and the original MWM/Coach definitions are not changed by this preset. No evidence of improved animation quality is claimed until an actual comparison is run.
