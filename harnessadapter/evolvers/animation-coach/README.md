# Animation Coach

An animation-focused Evolver for the MWM adapter architecture. It changes the learning objective, not the acting model: retain observations, scoped implementation guidance, explicit design hypotheses, and validated LearningMaterial that a fresh coding agent can use.

This is a new method variant, not the original MWM method and not a demonstrated quality improvement. It does not contain a prewritten ocean recipe or copy the prior animation into the adapter.

## Use

```sh
harnessadapter evolve --architecture mwm \
  --evolver animation-coach \
  --learning-material /path/to/learning-material \
  --model gpt-5.6-sol --reasoning-effort high \
  --no-network --timeout 1200 \
  --output /path/outside-framework/evolution
```

Replace `--architecture mwm` with a learned adapter directory to continue. Add `--prepare-only` to construct and inspect the session without a model call. The preset's network default follows the framework default (enabled); `--no-network` above keeps an experiment limited to supplied LearningMaterial. Validation is optional. Model and reasoning settings are explicit run settings, not hardcoded into the method.

LearningMaterial needs a README describing its records. Useful inputs are the task, source artifact, execution trajectory, timestamped screenshots, and optional human feedback or measurements. Read_image supports PNG/JPEG up to 4 MiB. Images must actually be opened by the learner; presence on disk is not inspection. Sampled frames cannot establish continuous motion quality.

The output is a separate candidate adapter plus run records. No automatic publication, activation, quality score, or new animation generation occurs. Deploy the candidate to a fresh acting run separately.

## Files and behavior

- PROMPT.md: learning objective and instruction precedence.
- materials/CONTRACT.md: review workflow, knowledge status, briefing, and reporting.
- METHOD.md: access and runtime bridge.
- evolver.toml: Codex default and shared MWM lifecycle selection.

Memory, examples, briefing, tools, and native roles keep their current MWM interfaces. The original MWM core/agenda/learning skills are not loaded; journal rolling, docket limits, and final executable admission are reused. There is no separate optimizer state implementation in this preset. The original `mwm` Evolver remains available.

Learned executable tools still require the existing Linux bubblewrap backend. This method does not make that backend available on macOS or force acting agents to call wm. Observations, hypotheses, and validated LearningMaterial are writing conventions, not machine-enforced guarantees.

## Development-checkout note

Use an output directory outside the framework checkout, as in the command above. In the current editable installation, the default `.demo/` location can overlap a runtime-readable root; the sandbox preflight rejects that layout because protected control files become readable. An external output directory passed the macOS read/write/network preflight. No permissions were broadened to bypass this check.
