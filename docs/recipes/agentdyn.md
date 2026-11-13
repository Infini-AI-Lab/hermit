# AgentDyn defense bundles

[EvoSafeHarness](../methods/evosafe.md) learns a system prompt and executable defense hooks around AgentDyn's
frozen victim. The native Shopping, GitHub and DailyLife loops remain task code; all model calls use the `direct`
Base Agent. Designer, Criticizer and Analyzer sessions use the shared Claude or Codex Base Agent.

The [example README](https://github.com/Infini-AI-Lab/hermit/tree/main/examples/agentdyn) describes installation
and case preparation. `examples/agentdyn/evosafe/run_nemotron_codex.yaml` names the frozen victim backend,
learning CLI, training IDs and budgets. The Codex configuration uses disjoint training and held-out test
groups; the adapter is frozen before testing.

## Results

See the [No-defense and EvoSafeHarness comparison](https://github.com/Infini-AI-Lab/hermit/blob/main/examples/agentdyn/evosafe/README.md#results).
The Lightning comparison covers 186 fixed TEST cases and indirect injections, with runtime errors reported
separately. Both Codex and Claude recipes use the same victim and keep `max_iters: 25`; the measured
comparison uses Codex roles. It is for reference only: the learner ran without Hermit's sandbox, so it is not a
reported result.
