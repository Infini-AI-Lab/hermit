# Harness Adapter documentation

Add an independently evolvable capability to an existing agent. Start with a prompt-only architecture or the MWM reference architecture, learn from LearningMaterial, and deploy a candidate to Codex or Claude Code.

This is a local prototype. Its CLI and execution boundary work; a public package release, general SDK, automatic publication, and framework integrations remain planned.

## Start here

- [Quickstart](quickstart.html): prepare an acting host or generate a separate candidate.
- [Components](components.html): architecture, adapter, acting harness, Evolver, and LearningMaterial.
- [Architectures](architectures.html): choose Prompt-only or MWM.
- [Deployment](deployment.html): context, optional MCP tools, and optional native roles.
- [Evolution and LearningMaterial](evolution.html): prompt-driven updates, permissions, and optional validation.
- [Optimizer Evolvers](optimizer-evolvers.html): GEPA and SkillOpt through `evolve`, the Environment contract, `evaluate`, and writing your own plugin.
- [Configuration](configuration.html): CLI and YAML reference.
- [Status and limits](status.html): what is implemented and what remains open.

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
