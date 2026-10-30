# OfficeQA (SkillOpt paper setting)

Questions about U.S. Treasury Bulletins (1939-2025). The agent gets the question with the parsed pages its answer
comes from, can read and search the question's source files, and must answer inside `<answer>...</answer>`.
Scoring is exact match after normalisation (lower case, commas, punctuation and unit words removed), no tolerance.
The agent works in a directory of its own: the adapter's `ADAPTER.md` is its instructions there (as `AGENTS.md`),
and every other file of the adapter is placed in that directory at the same relative path. The names `AGENTS.md`,
`task.md` and `docs` at the adapter's root are taken by the session itself.
