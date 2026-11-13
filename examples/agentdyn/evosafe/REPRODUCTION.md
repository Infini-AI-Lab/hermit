# EvoSafeHarness reproduction

This is a fixed-case AgentDyn variant of EvoSafeHarness. The reference is
[domain_spec.md at 98a4a96](https://github.com/SaFo-Lab/EvoSafeHarness/blob/98a4a96d61f2b6e040c1f677864073eabaef5311/domains/os-filesystem/domain_spec.md),
sections 0 and 3–5 and their linked experiences.

| Component | This implementation and difference from the reference |
|---|---|
| Adapter | Retains prompt and executable pre/post hooks with per-episode state. A portable, standard-library SDK replaces DTAP imports to support AgentDyn and isolated execution. |
| Designer / Criticizer | Allows a coherent redesign across prompt, pre/post hooks and session state, without a small-edit or no-growth preference. Uses at most three fresh independent feedback calls per candidate. After the last feedback, a final Designer repair proceeds to empirical evaluation once hard size/specification checks pass, without a new approval. The last review verdict remains recorded. This bounded feedback policy differs from a blocking review gate. The executable search budget defaults to 32,000 UTF-8 bytes, capped by the template limit. |
| Archive / parents | The accepted parent remains the measured reference. CLI roles can browse and page through a persistent read-only TRAIN archive containing all registered candidates, source revisions, outcomes, complete observable trajectories/audits, reviews and lessons. Rejected source also reaches the next Designer. Existing initial summaries and highlighted dossiers are navigation aids, not the only available observations. Automatic import of unrelated runs and archive-based parent selection remain omitted. |
| Warm start | Retains a generic trust-boundary prompt, identity hooks and selected scope/provenance principles. Executable CaMeL/DRIFT seeds and complete experience materials are not ported because their task APIs differ. |
| Classifier | Uses the same frozen victim with bounded, side-effect-free calls. The operator fixes its prompt, interface and budget; the hook learns when and what to classify. |
| Selection | Retains static checks, independent review, a training cascade. The Codex configuration uses training-only promotion and reserves held-out testing until training finishes. These thresholds are selection heuristics. |
| Robustness | Uses conceptual review and fixed held-out reporting after training. Adaptive, renamed-payload and over-refusal probes are omitted, limiting claims to the fixed cases. |

## Execution boundary

Learned Python runs in a separate Docker container with no network, credentials or writable
host mounts, a read-only root filesystem, and bounded resources. Only defense source, the SDK
and the worker are mounted. Native tools and judging remain in a trusted process that does
not import learned code. The boundary uses bounded JSON RPC; unchanged native observations
retain their original values, while rewritten observations must be text. Docker and the host
kernel are trusted infrastructure.

The supervisor enforces an explicit 900-second episode deadline because the direct function
interface does not propagate `agent.timeout_min`. Timeouts are errors with potentially unknown
usage/cost. [EXPERIMENT_RUNTIME.md](EXPERIMENT_RUNTIME.md) describes source freezing, formal
entry checks and cancellation cleanup. Learner orchestration still runs on the host.

## Optional Daybreak for Codex roles

When Codex optimizes the defense, requests that include attack examples or security
analysis can trigger cybersecurity checks and be refused. If your account has Daybreak
access and your Codex CLI and model support it, we recommend enabling Daybreak for
authorized defensive experiments to reduce unnecessary refusals.

Append `-c` and `daybreak=true` as two entries to `evolver.codex.args` in the
[Codex recipe](run_nemotron_codex.yaml), keeping the existing entries. EvoSafeHarness
leaves this option unset by default. Record whether you enabled it with the run
configuration. See [Using Daybreak in Codex CLI](https://help.openai.com/en/articles/20001607-using-daybreak-in-codex-cli)
for access and setup requirements.

## Evaluation

The [Codex recipe](run_nemotron_codex.yaml) uses Nemotron 3.5 Lightning as the frozen victim
and GPT-6 Sol with high effort for the learner roles. It uses 57 training cases and 186
held-out cases from `full-test-v1`, grouped by user task. Candidate selection uses the training cascade.
The [Claude recipe](run_nemotron_claude.yaml) uses the same Lightning victim and TEST set, with a disjoint
validation split for candidate confirmation. Both recipes keep `max_iters: 25`.
The dataset revision, construction and split hashes are in [DATA.md](../utils/DATA.md).

The experiment runner evaluates `none`, `root`, and `final` on the same test split
with the same victim settings. Clean utility is measured on completed clean cases.
ASR is measured on completed injected cases. The [aggregate results](README.md#results) report the
186-case Lightning comparison for `none`, `root` and `final`, with episode errors and the run's sandbox
exception stated separately. These measured results use Codex roles; Claude-role results are not yet available.
They are for reference only: the learner ran without Hermit's sandbox, so they are not a reported result.

For a reproducible comparison, retain the manifest, effective configuration, adapter
snapshots, and per-task records together in the run's ignored `artifacts/` directory.
Use `comparison.json` to inspect episode coverage, descriptive intervals, and paired
clean-task changes.
