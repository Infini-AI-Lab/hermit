# CoEvoSkills Adapter harness adapter

`skills/<task>-evo-<name>/` holds the executable Skill packages the CoEvoSkills loop exported, one directory per
package (a `SKILL.md` with its scripts), named after the training task it was learned on. The host installs them as
its native skills (Codex: `.codex/skills/` of the session's workspace), so the acting agent sees every package's name
and description and opens and runs the ones it chooses. `ADAPTER.md` is the starting context.
