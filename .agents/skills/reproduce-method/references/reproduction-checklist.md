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
- [ ] **Upstream fidelity and adaptations.** Compare roles, prompts, learning
  evidence, update/selection rules and search space with the pinned upstream
  implementation. Adaptations are allowed: for each substantive difference,
  state what changed, why (framework constraints, task requirements or a
  reproduction choice), and its effect on behavior and result comparability.
- [ ] **Learning information and evaluation boundaries.** Inspect manually written
  prompts, task descriptions and seeds for held-out examples, answers or targeted
  solution guidance. Verify that training evidence and labels follow the declared
  protocol. Check the execution boundary between learned code and scoring logic,
  and disclose any access or isolation limitations.
- [ ] **Experiments.** What was run, and on what? State the benchmark and its data
  (split or stream), protocol, acting agent and its model, Evolver and its model,
  budgets and number of runs. Verify that baselines and comparisons use matching
  settings; explain deviations. Link the accessible experiment code revision,
  configuration, final adapter and per-task records. Report errors, metric
  denominators and uncertainty for single-run results.
- [ ] **Agent three-layer abstraction.** Does the reproduction follow the current
  [Model → Base Agent → Acting / Evolver Agent abstraction](../../../../docs/framework/agent-layers.md)?
  Identify the layers for each acting and learning role, and name the Base Agent
  and shared API used for each model or CLI call (including the framework's `llm`
  tool or `ChatEndpoint` where appropriate). Trace actual calls, including auxiliary
  models and runtime wrappers, and check for overrides of shared execution behavior.
  Explain how it reuses session,
  configuration, login-plan, timeout, cancellation and usage-accounting behavior.
  If a capability is missing, explain how the shared layer should be extended
  rather than duplicating it in a method-specific caller.

For the general checklist's core-change and documentation answers, use the guide's
[implementation boundaries](../../../../docs/developer/reproducing-a-method.md#1-what-the-adapter-is-and-what-the-evolver-is)
and [verification and documentation requirements](../../../../docs/developer/reproducing-a-method.md#5-verification-in-order).
