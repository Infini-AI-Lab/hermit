# Sandbox checklist

Answer these items alongside the [general checklist](general-pr-checklist.md) when a pull request that adds or
changes a method or a dataset starts a process or changes what a sandbox shows. The design is in
[How the sandbox works](../../../../docs/guides/sandbox.md).

- [ ] **Reuse the framework's sandbox.** Does every process the change starts (agent, judge, learner and its roles,
  tools, the adapter's code) run in a confined environment the framework gives (`hermit.environment`: `Sandboxed`,
  `Container`, or a task's Harbor environment) or through `hermit.sandbox` / `hermit/isolate.py`, with its needs
  declared in the existing settings (`environment.*`, `judge.*`, a task's `task.toml`, the method's view,
  `sandbox.evolver.*`)? `Local` is not confinement: it runs on this machine as it is. Link the evidence from a run of
  the change that the confinement was enforced: a launch record (`sandbox/<role>-<n>.json` whose isolation probe
  passed, `probe.passed: true`), or, where the framework keeps no launch record (a container, a Harbor trial with its
  `trial_dir` in the rollout's verdict, the strict adapter sandbox), a check run inside it that shows what it can read,
  write and reach. For each process that runs otherwise (`Local`
  included), state why and how it is confined instead, or, for trusted code on the host (a judge that only reads a
  verdict), exactly what it does.
- [ ] **Sandbox changes are core changes.** Does the pull request change the sandbox itself (`hermit/sandbox.py`,
  `hermit/sandbox_relay.py`, `hermit/isolate.py`, `hermit/environment/`, the Trainer's evolver sandbox, or their
  defaults)? If so, link the issue where the change was agreed with the core team, and answer it under **Core
  changes** in the general checklist.
