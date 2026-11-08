# Sandbox checklist

Answer these items alongside the [general checklist](general-pr-checklist.md) when a pull request that adds or
changes a method or a dataset starts a process or changes what a sandbox shows. The design is in
[How the sandbox works](../../../../docs/guides/sandbox.md).

- [ ] **Reuse the framework's sandbox.** Does every process the change starts (agent, judge, learner and its roles,
  tools, the adapter's code) run through `hermit.sandbox` or `hermit/isolate.py`, with its needs declared in the
  existing settings (`environment.*`, `judge.*`, the method's view, `sandbox.evolver.*`)? Link one
  `sandbox/<role>-<n>.json` from a run of the change. For each process that cannot use this abstraction, state why
  and how it is confined instead.
- [ ] **Sandbox changes are core changes.** Does the pull request change the sandbox itself (`hermit/sandbox.py`,
  `hermit/sandbox_relay.py`, `hermit/isolate.py`, the Trainer's evolver sandbox, or their defaults)? If so, link the
  issue where the change was agreed with the core team, and answer it under **Core changes** in the general
  checklist.
