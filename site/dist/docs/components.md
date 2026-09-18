# Components and the training analogy

The Acting Harness solves tasks. The Harness Adapter supplies independently maintained context and capabilities. The Meta Harness runs the Evolver that changes an adapter candidate.

## A familiar analogy

| Model training | Harness Adapter framework |
| --- | --- |
| Base model | Existing acting agent and its harness |
| Model architecture | Adapter Architecture: starting files, representation, and interfaces |
| Trained adaptation module | Evolved Adapter: current prompt, knowledge, code, and optional roles |
| Training algorithm | Evolver: instructions and an update procedure |
| Training data | Experience: histories, observations, or feedback |
| Validation data | Optional validation storage or method-specific evaluation |
| Model checkpoint | Adapter: the learned capability artifact |
| Optimizer state | Optional Training State: internal progress for continuing evolution |
| Inference | Deploying and using the adapter |

This is a conceptual analogy. Adapters contain text and executable code rather than numerical weight deltas. Evolution can change the structure itself; it is not constrained to a fixed architecture or gradient descent. A recorded revision is not yet an automatic activation or rollback service.

## One iteration, two outputs

```text
Adapter + Experience + Configuration + Training State (optional)
                              |
                           Evolver
                              |
                 +------------+------------+
                 |                         |
          Updated Adapter       Updated Training State (optional)
                 |                         |
         Deploy to acting agent     Resume future evolution
```

The Evolver is the reusable algorithm definition. Training State is a separate input/output artifact, not part of its shared implementation. Its format belongs to that Evolver; the framework need not prescribe one knowledge or optimizer-state schema.

A stateless Evolver returns training_state = None. A stateful Evolver may retain candidate scores, search history, processed-experience progress, or other algorithm-specific information. State compatibility must be checked when changing Evolver implementations or versions; an arbitrary state artifact is not interchangeable across methods.

Deploying or sharing an Adapter does not require Training State. To continue a previous optimization process, optionally provide compatible state alongside the Adapter. A candidate can be rejected for deployment while its attempted update still contributes useful search history; publication and state retention are separate decisions.

Training State is also distinct from the Adapter's own persistent knowledge or runtime state, and from framework audit logs. This distinction does not automatically relocate the current MWM journal or cases.

This is the agreed protocol design. The current CLI produces a candidate and execution records; a general Training State input/output, persistence, and resume API is not implemented yet. No stateful session object is mandatory for all Evolvers.

## Responsibilities

| Component | Owns |
| --- | --- |
| Adapter core | Context, knowledge, capabilities, role definitions, and declarations |
| Runtime | Loading core exports, MCP transport, resource binding |
| Host connector | Preparing context, tool registrations, and native role files for a host |
| Evolver | How experience is interpreted and how the candidate changes |
| Evolution infrastructure | Experience snapshots, writable boundaries, execution checks, and result records |

The connector is optional in the abstraction. The current CLI provides connectors for Codex and Claude Code. External frameworks can eventually use the same core operations while retaining their own scheduling and publication policy; the public SDK and Reef integration are not implemented yet.

## Three documents, three readers

- README.md explains the adapter to a human user.
- ADAPTER.md, or a declared renderer, supplies context to the acting agent.
- EVOLVER.md explains the adapter's structure and update conventions to the learner.

Experience has its own README.md. None of these documents grants filesystem or network permissions; framework configuration determines those.
