# Experiment startup, source snapshot and cancellation

Run `../utils/run_experiment.py --config run_nemotron_codex.yaml` only after committing the intended
source changes and choosing unused run/tree paths. The runner does not start or resume an experiment merely
because this document or its tests are read.

On a host without usable bubblewrap, `--no-sandbox` explicitly disables the learner sandbox for this run.
The launcher records `sandbox.enabled=false` in the frozen effective configuration and `runtime_overrides`
metadata, while preserving the committed recipe. Learned hooks retain their separate Docker boundary.
This override must be applied at launch, before source freezing; it cannot modify an existing frozen run.

The launcher exports the exact Git commit into a private directory under `runs/.source-snapshots/`. It copies
the prepared dataset and splits, uses committed task information and template files, freezes the effective
configuration and private backend settings, and redirects outputs to the original run/tree/log locations.
The snapshot files are read-only. A new interpreter executes the complete runner from that snapshot with
its own `PYTHONPATH`. Editing the original checkout cannot change late imports in the measured process.
File hashes are checked at snapshot entry and before/after each experiment stage. The manifest records
the source commit, snapshot location and hashes. Third-party installed dependencies are recorded by version;
they are not vendored by this mechanism. This is protection against source drift, not a sandbox against the
operator deliberately replacing a snapshot or installed dependencies.

Before any learner request or victim rollout, a fresh subprocess loads the configured Evolver through
Hermit's actual loader and imports the selected role-search entry, proposal schema and review/analyzer
schemas. It exits immediately on an import or configuration failure. The separate structured CLI preflight
then checks provider access. Neither check proves that all later role requests will be accepted.
The role preflight resolves backend bindings through the same Evolver loader as training. The manifest
records the configured role model and identifies the source of reported model IDs. Claude events can carry
those IDs. The Codex exec JSON event stream records usage and session IDs.

The launcher and frozen runner handle SIGINT and SIGTERM. Each stage has an owned process group; cleanup
also enumerates descendants in separate sessions, sends termination followed by kill, and reaps the direct
child. Hook containers register their unique names in the experiment's private registry. Cleanup removes
only those registered containers and verifies that none remain. Additional signals are deferred during
cleanup. An interrupted stage/run is recorded as `cancelled`, rather than a successful experiment.
SIGKILL and machine/power failure cannot run Python cleanup; retain the private snapshot and registry for
manual recovery in that situation.

Private backend settings remain inside the private snapshot and are not copied into the public artifact
bundle. Review artifact contents before publication. Completed or cancelled snapshots remain available for
inspection; delete them explicitly when disposing of their corresponding experiment.
