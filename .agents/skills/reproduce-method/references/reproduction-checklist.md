# Method reproduction checklist

Answer these items alongside the [general checklist](../../prepare-pr/references/general-pr-checklist.md),
with links to supporting files, runs or records.

- [ ] **Adapter template.** Does the pull request add a new adapter template or
  use an existing one? Which one, and if it is new, why does no existing template
  fit? What may the learner change, what is fixed, and what reaches the acting agent?
- [ ] **Evolver.** Does the pull request add a new Evolver or use an existing
  one? Draw one step as a flow chart (a Mermaid diagram in the description is
  enough): what the Evolver reads, what it calls (the learner, tools and rollouts),
  what it writes, and how the final adapter is chosen.
- [ ] **Experiments.** What was run, and on what? State the benchmark and its data
  (split or stream), protocol, acting agent and its model, Evolver and its model,
  budgets, number of runs, and results against baselines measured on the same settings.
- [ ] **Agent three-layer abstraction.** Does the reproduction follow the current
  [Model → Base Agent → Acting / Evolver Agent abstraction](../../../../docs/framework/agent-layers.md)?
  Identify the layers for each acting and learning role, and name the Base Agent
  and shared API used for each model or CLI call (including the framework's `llm`
  tool or `ChatEndpoint` where appropriate). Explain how it reuses session,
  configuration, login-plan, timeout, cancellation and usage-accounting behavior.
  If a capability is missing, explain how the shared layer should be extended
  rather than duplicating it in a method-specific caller.

For the general checklist's core-change and documentation answers, use the guide's
[implementation boundaries](../../../../docs/developer/reproducing-a-method.md#1-what-the-adapter-is-and-what-the-evolver-is)
and [verification and documentation requirements](../../../../docs/developer/reproducing-a-method.md#5-verification-in-order).
