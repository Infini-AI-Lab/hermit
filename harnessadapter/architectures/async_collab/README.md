# Async collaboration adapter

Design: `docs/async-collab-adapter-design-2026-09-27.md`. Successor of `lemma_board`.

Shared state (COLLAB_DIR): `state.json` (lemmas, subgoals, agents, events, messages), `verified/*.lean`,
`channel.jsonl`, `index.md` (rendered view). Tools: `check` (compiles the caller's file, extracts verified
helpers into the library and sorry helpers into the subgoal map, infers what the caller is working on),
`pull`, `refute`, `say`. Every reply carries the `[team]` delta since the caller's last call.

Environment expected by the tools (set per agent by the launcher on the MCP server):
COLLAB_DIR, COLLAB_AGENT, COLLAB_WORKSPACE, COLLAB_TARGET, COLLAB_REPO, COLLAB_LAKE (plus ELAN_HOME / PATH / HOME
for lake). `resume_text(dir, agent)` renders the launcher's resume message.
