# Overview

Add an independently evolvable capability to an existing agent. Start with a prompt-only architecture or the MWM reference architecture, learn from LearningMaterial, and deploy a candidate to Codex or Claude Code.

This is a local prototype. Its CLI and execution boundary work; a public package release, general SDK, automatic publication, and framework integrations remain planned.

## Start here

- [Quickstart](quickstart.md): prepare an acting host or generate a separate candidate.
- [Components](components.md): architecture, adapter, acting harness, Evolver, and LearningMaterial.
- [Architectures](architectures.md): choose Prompt-only or MWM.
- [Deployment](deployment.md): context, optional MCP tools, and optional native roles.
- [Evolution and LearningMaterial](evolution.md): prompt-driven updates, permissions, and optional validation.
- [Configuration](configuration.md): CLI and YAML reference.
- [Status and limits](status.md): what is implemented and what remains open.

## One lifecycle

```text
Adapter or Architecture + LearningMaterial + Config + optional Training State
                              |
                           Evolver
                              |
                 +------------+------------+
                 |                         |
          Candidate Adapter     Training State (optional)
                 |                         |
           review / deploy          next iteration
```

Training State is an optional protocol output, similar to optimizer state. Its generic persistence and resume interface is planned; the local prototype currently returns a candidate and execution records.


The framework supplies infrastructure. An adapter author chooses the internal representation; an Evolver chooses how LearningMaterial changes it. MWM is one implementation, not a mandatory storage schema.
