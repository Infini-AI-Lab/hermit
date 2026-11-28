# OfficeQA (Harbor's task set, the paper's evaluation)

Questions about U.S. Treasury Bulletins (1939-2025). The agent has the whole corpus under `/app/corpus/` (697 parsed
bulletins) and is asked in the OfficeQA paper's agent prompt, which ends its final message with the answer inside
`<FINAL_ANSWER>...</FINAL_ANSWER>`. The verifier scores the first such answer with the OfficeQA paper's scorer: the
expected number found in it, correct only within a 0.0% relative error (exact after the scorer's normalisation of
formats and units). The adapter's `ADAPTER.md` is the agent's instructions: a block of the working directory's
`AGENTS.md` (Codex) or `CLAUDE.md` (Claude Code).

A rollout's feedback never holds the expected answer: `passed` for a right answer; else `reward 0` and the tail of the
verifier's output, `FAIL: final answer '…'` (with `(no <FINAL_ANSWER> tag)` when the message had none, the whole
message then being the answer) or `FAIL: no final message`. Its record's `judge.extra.rewards` also holds the verdicts
at 0.1%, 1% and 5%.
