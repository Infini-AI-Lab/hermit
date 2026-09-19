# Evolution and experience

An Evolver can be just a prompt and configuration. It runs through Codex or Claude Code, reads the adapter guide and experience README, and updates a separate candidate.

```sh
harnessadapter evolve --architecture prompt-only \
  --evidence /path/to/evidence \
  --prompt-file /path/to/update-instructions.md \
  --backend codex --no-network
```

To continue from a learned artifact, replace --architecture prompt-only with the candidate directory. Architecture initialization and continued evolution use the same update interface.

## Optional Training State

The iteration contract is: Adapter + Experience + Configuration + optional Training State → updated Adapter + optional updated Training State. Stateless Evolvers return no Training State; stateful methods define their own format.

Training State is optimizer-like information for the next update, such as candidate history or search progress. It stays separate from the reusable Evolver definition and the deployable Adapter. Acting hosts never need it just to use the Adapter.

This contract is a design decision, not an available CLI flag. The current prototype returns a candidate and run records. Generic state input/output and resume support are planned; a successful run log is not automatically an optimizer checkpoint.

## Experience storage

Experience is the public concept name. The current CLI flag remains `--evidence`, the launch YAML key remains `evidence`, and the internal session area remains `evidence/`. They all refer to the supplied Experience storage; this terminology update does not change the runtime interface.

Provide a directory containing README.md and records in your own format. The README should explain filenames, fields, chronology, feedback meaning, missing information, and which facts were actually observed. The framework copies the storage without converting it into an MWM schema.

The candidate is writable. Experience, previous revision, and method materials are read-only. Scratch is writable for temporary work. Additional environment reads are explicitly configured.

```sh
harnessadapter evolve /path/to/adapter --evidence /path/to/evidence \
  --prompt "Investigate the supplied records and update the adapter." \
  --workdir /path/to/environment --read-dir /path/to/reference
```

Here --workdir is an environment directory: readable, used as command cwd, but not writable. --read-all broadens reads only. Network is enabled by default; --no-network disables agent tool and command networking while retaining model inference transport.

## Optional validation

```sh
harnessadapter evolve --architecture prompt-only \
  --evidence /path/to/evidence --validation /path/to/validation \
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

## Enforcement and outputs

Framework tools enforce the read/write and network policy using an OS sandbox. Both learner backends currently require the Codex CLI as the shared sandbox executor. Authenticated client transport and vendor bookkeeping remain a trusted control plane; this is not a whole-client-process isolation claim.

Each session records the candidate, input snapshots, checks, tool audit, report, and result. candidate_ready means the run completed and structural checks passed. It does not mean measured improvement. No candidate is automatically activated.
