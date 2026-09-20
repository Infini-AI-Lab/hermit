# Evolution and LearningMaterial

An Evolver can be just a prompt and configuration. It runs through Codex or Claude Code, reads the adapter guide and LearningMaterial README, and updates a separate candidate.

```sh
harnessadapter evolve --architecture prompt-only \
  --learning-material /path/to/learning-material \
  --prompt-file /path/to/update-instructions.md \
  --backend codex --no-network
```

To continue from a learned artifact, replace --architecture prompt-only with the candidate directory. Architecture initialization and continued evolution use the same update interface.

## Optional Training State

The iteration contract is: Adapter + LearningMaterial + Configuration + optional Training State → updated Adapter + optional updated Training State. Stateless Evolvers return no Training State; stateful methods define their own format.

Training State is optimizer-like information for the next update, such as candidate history or search progress. It stays separate from the reusable Evolver definition and the deployable Adapter. Acting hosts never need it just to use the Adapter.

This contract is a design decision, not an available CLI flag. The current prototype returns a candidate and run records. Generic state input/output and resume support are planned; a successful run log is not automatically an optimizer checkpoint.

## LearningMaterial storage

LearningMaterial is the public name for task inputs, source artifacts, execution records, screenshots, user feedback, and other material supplied to an Evolver. Use `--learning-material` in the CLI, `learning_material` in launch YAML and Python, and `area="learning_material"` in evolution tools. Legacy `--evidence`, YAML `evidence`, Python `evidence=`, and tool area `evidence` remain compatibility aliases. Supplying both input names is rejected.

Session snapshots still use the physical `evidence/` directory and old audit fields for compatibility with existing adapters, pinned MWM instructions, and archived runs. Both tool-area names refer to this same read-only directory; no duplicate data is created and historical runs are not renamed.

Provide a directory containing README.md and records in your own format. The README should explain filenames, fields, chronology, feedback meaning, missing information, and which facts were actually observed. The framework copies the storage without converting it into an MWM schema.

The candidate is writable. LearningMaterial, previous revision, and method materials are read-only. Scratch is writable for temporary work. Additional environment reads are explicitly configured.

```sh
harnessadapter evolve /path/to/adapter --learning-material /path/to/learning-material \
  --prompt "Investigate the supplied records and update the adapter." \
  --workdir /path/to/environment --read-dir /path/to/reference
```

Here --workdir is an environment directory: readable, used as command cwd, but not writable. --read-all broadens reads only. Network is enabled by default; --no-network disables agent tool and command networking while retaining model inference transport.

## Optional validation

```sh
harnessadapter evolve --architecture prompt-only \
  --learning-material /path/to/learning-material --validation /path/to/validation \
  --prompt "Follow the evidence and validation READMEs; report checks performed."
```

Validation storage must be separate and contain README.md. It is copied into an additional read-only area only when supplied. The learner can inspect it: this is not hidden held-out evaluation. Binding data does not automatically run an evaluator, calculate a score, or select a better candidate.

An Evolver definition may set requires_validation = true. The framework then rejects runs without it before invoking a model. A method decides how validation is used; performance evaluation remains method-specific.

## Reusable Evolvers

```text
my-evolver/
  evolver.toml
  PROMPT.md
```

```toml
prompt = "PROMPT.md"
requires_validation = false
[execution]
backend = "codex"
network = true
[execution.read_access]
paths = []
```

Pass --evolver /path/to/my-evolver to reuse it. The bundled MWM preset additionally uses method lifecycle code. Arbitrary Python lifecycle plugins are not yet a public interface.

### Animation Coach

`--evolver animation-coach` is an animation-focused variant for the MWM architecture. It learns scoped implementation strategies and explicitly unverified design hypotheses alongside observations and validated experience. It reuses MWM journal/docket maintenance and executable checks, but loads its own learning contract instead of the environment-only MWM policy. It does not require human feedback or validation data, force acting tool calls, or claim that each update improves quality.

```sh
harnessadapter evolve --architecture mwm --evolver animation-coach \
  --learning-material /path/to/learning-material \
  --model gpt-5.6-sol --reasoning-effort high --no-network --timeout 1200 \
  --output /path/outside-framework/evolution
```

Replace `--architecture mwm` with a learned adapter path to continue. The output is a candidate; deploy it in a separate acting run to generate the next animation. No animation experiment runs automatically. Full method instructions live in `harnessadapter/evolvers/animation-coach/`.

### Physics Builder

`--evolver physics-builder` reuses MWM storage and maintenance with a different learning objective: diagnose a consequential failure, investigate competing causes with available experiments, and deliver scoped knowledge, computational tools, diagnostic checks or reusable source components. It does not require creating a tool every iteration. Its full core prompt and runtime limitations live in `harnessadapter/evolvers/physics-builder/`.

All assistance is for animation construction. Runtime code must be integrated into the final deliverable, which must not depend on WM/MCP during playback. This preset does not fix the existing macOS learned-tool admission limitation: scratch diagnostics and retrievable code are distinct from admitted executable tools. An actual experiment is needed to establish utility.

## Enforcement and outputs

Framework tools enforce the read/write and network policy using an OS sandbox. Both learner backends currently require the Codex CLI as the shared sandbox executor. Authenticated client transport and vendor bookkeeping remain a trusted control plane; this is not a whole-client-process isolation claim.

Each session records the candidate, input snapshots, checks, tool audit, report, and result. candidate_ready means the run completed and structural checks passed. It does not mean measured improvement. No candidate is automatically activated.

## Visual LearningMaterial

The Evolver can call read_image(area="learning_material", path="observations/frames/frame_000.png") to receive an actual MCP image block. PNG and JPEG are supported up to 4 MiB each. Reads run through the same OS sandbox and workspace containment checks as text reads; traversal and links to unauthorized files are rejected. Image hashes and sizes are logged without duplicating image data in the audit. Native unrestricted view_image remains disabled.

A sequence should include a frame manifest with capture timestamps, order, viewport and pixel dimensions. These are sampled images, not native video input, and may miss fast events. Merely placing a file in the LearningMaterial directory does not show it to the model: it must call read_image.

Use --model gpt-5.6-sol --reasoning-effort high for an explicit Codex learner configuration. The reasoning-effort option currently applies only to Codex.
