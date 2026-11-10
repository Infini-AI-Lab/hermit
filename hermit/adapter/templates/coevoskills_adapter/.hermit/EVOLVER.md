# CoEvoSkills Adapter

The Evolver installs the packages the upstream loop exports as `skills/<task>-evo-<name>/`, each checked against
upstream's Skill schema, and replaces only the skills of the task it learned on. `ADAPTER.md` (the starting context)
and `exports.yaml` are not changed. A package carries no hidden tests, answers, histories or generator artifacts.
