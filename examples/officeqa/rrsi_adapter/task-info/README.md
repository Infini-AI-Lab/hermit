# OfficeQA (SkillOpt paper setting)

Questions about U.S. Treasury Bulletins (1939-2025). The agent gets the question with the parsed pages its answer
comes from, can read and search the question's source files and the whole corpus (under `docs/`, read-only), and
must answer inside `<answer>...</answer>`. Scoring is exact match after normalisation (lower case; commas and
punctuation other than the numeric characters and `%` removed; the words million(s), billion(s), dollar(s) and
nominal removed), no tolerance.

The agent is Codex in a container of its own, working in a directory of its own, which holds the question
(`task.md`) and the corpus (`docs/`): the adapter's `ADAPTER.md` is its instructions there (in `AGENTS.md`), and every
other file of the adapter is copied under `hermit/rrsi-adapter/` at the same relative path and listed in `AGENTS.md`
by its path (a Markdown, text or Python file with its first line). Paths in `ADAPTER.md` are relative to that folder, which the instructions say; a command the
agent runs from them is run there. So a helper script or a notes file reaches the agent only when `ADAPTER.md` tells
it what is there and when to use it. The session's commands have no network. An `exports.yaml` at the adapter's root
connects what it declares to Codex (skills to `.codex/skills/`, roles to `.codex/agents/`, command hooks to
`.codex/hooks.json`); Python it names (a tool, an exporter) would run outside the agent's container and is refused
before measurement. In this setting a reserved exploration slot does not ask for a hook or a role (the components
control_flow, output_plumbing, context_mgmt and subagent): the reported runs had no carrier for them. A
`memory/NOTES.md` makes the agent's prompt call it a memory to record into across questions, which this setting does
not keep: keep notes the agent reads under another name. Each question runs in a fresh directory: nothing the agent
writes during one question reaches another.
