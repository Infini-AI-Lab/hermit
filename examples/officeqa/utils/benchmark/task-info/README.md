# OfficeQA (SkillOpt paper setting)

Questions about U.S. Treasury Bulletins (1939-2025). The agent gets the question with the parsed pages its answer
comes from, can read and search the question's source files, and must answer inside `<answer>...</answer>`.
Scoring is exact match after normalisation (lower case, commas, punctuation and unit words removed), no tolerance.
The adapter's ADAPTER.md is given to the agent as its skill: in the prompt in direct chat, as the session's
`AGENTS.md` in the Codex harness, where the question is in `task.md` and the whole corpus under `docs/`.
