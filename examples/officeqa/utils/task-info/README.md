# OfficeQA (SkillOpt paper setting)

Questions about U.S. Treasury Bulletins (1939-2025). The agent gets the question with the parsed pages its answer
comes from, can read and search the question's source files, and must answer inside `<answer>...</answer>`.
Scoring is exact match after normalisation (lower case, commas, punctuation and unit words removed), no tolerance.
The adapter's ADAPTER.md is given to the agent as its skill.
