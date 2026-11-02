# OfficeQA (SkillOpt paper setting)

Questions about U.S. Treasury Bulletins (1939-2025). The agent gets the question with the parsed pages its answer
comes from, can read and search the question's source files, and must answer inside `<answer>...</answer>`.
Scoring is exact match after normalisation (lower case, commas, punctuation and unit words removed), no tolerance.
The agent works in a directory of its own, which holds the question (`task.md`) and the corpus (`docs/`): the
adapter's `ADAPTER.md` is its instructions there (in `AGENTS.md`), and every other file of the adapter is copied
under `hermit/meta-harness-adapter/` at the same relative path and listed in `AGENTS.md` with its first line. Paths
in `ADAPTER.md` are relative to that folder, which the instructions say; a command the agent runs from them is run
there.
