# Lean collaboration template (`lean-collab@1.0`)

Migrated from the Lean collab adapter `final_v12_5` (fc_collab_auto). Its initial learnable content (ADAPTER.md,
skills/) is v12.5's. Design: `docs/async-collab-adapter-design-2026-09-27.md`.

Deployment: the exports in `adapter.yaml` (context, the five tools, `reactions`, `concurrent_tool_calls`, `skills`)
are served per run by `harnessadapter.runtime.service` (one MCP endpoint per agent) and deployed into each Codex
workspace with `harnessadapter deploy` / `harnessadapter.hosts.codex.prepare`; skills land in the workspace's
`.codex/skills/` (copy them to the agent's CODEX_HOME/skills if your Codex reads only that). Supported host: Codex.

Shared state (COLLAB_DIR): `state.json` (lemmas, subgoals, agents, events, messages), `verified/*.lean`,
`channel.jsonl`, `index.md` (rendered view). Tools: `check` (compiles the caller's file, extracts verified
helpers into the library and sorry helpers into the subgoal map, infers what the caller is working on),
`pull`, `refute`, `say`. Every reply carries the `[team]` delta since the caller's last call.

Environment expected by the tools (set per agent by the launcher on the MCP server):
COLLAB_DIR, COLLAB_AGENT, COLLAB_WORKSPACE, COLLAB_TARGET, COLLAB_REPO, COLLAB_LAKE (plus ELAN_HOME / PATH / HOME
for lake). `resume_text(dir, agent)` renders the launcher's resume message.

v3 (fc_collab_auto, 2026-09-27): two phases. `maps` in state.json (agents whose target proof compiles modulo sorry
helpers, with the parts); `phase_lines` head every tool reply, updates.md and index.md; `parts_view` ranks open parts
(free, stuck ≥ STUCK_MIN with an owner, freshly taken); per-part signals `checks`, `claimed`, `tried`; `check` returns
`phase`, `verified` (all of the caller's helpers in the library) and `advice` (write a map) in phase 1. Extraction:
`open … in` lines attach to their declaration, trailing `end`/`section` lines are dropped; dependencies are taken
through auxiliary constants and textual references; `_declared` knows every declaration keyword.

v4: scratch-file checks feed the library too (verified helpers only; parts and maps come from Problem.lean); only
axiom-clean lemmas (propext, Classical.choice, Quot.sound) enter; pulled names are recorded per agent and excluded
from working_on and from re-registration; a helper whose statement is already in the library is reported as a
duplicate ("pull X instead") rather than stored under a suffixed name; errors up to 60000 chars; unanswered
requests leave the digest after 30 min.
