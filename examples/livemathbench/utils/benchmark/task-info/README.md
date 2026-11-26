# LiveMathBench

Recent competition mathematics questions in English, from LiveMathBench (OpenCompass): its 202505 release for testing,
and its 202412 release (questions from the AMC, the Chinese college entrance examination, the Chinese Mathematical
Olympiad and the William Lowell Putnam competition) for training and validation. A question is a problem to solve or
fill in the blanks (several blanks, each answered); an answer may be a number, an expression, an interval, a set, a
phrase or several of these. The agent is given OpenCompass's prompt, "Here is a math question, please
reasoning step by step, and put your answer in \boxed{}.", followed by the question, and has an hour.

The agent is a coding CLI (Codex or Claude Code, as the run's config names) in a sandbox of its own, with a shell and
Python (the packages of the run's Python environment) and no network but its model's; web search is off. Its loop and
its tools are fixed; the adapter is what changes. The adapter is deployed into the working directory as `hermit
deploy` lays it out for that CLI: `ADAPTER.md` is the agent's instructions (a block of `AGENTS.md` for Codex,
`CLAUDE.md` for Claude Code), and the rest of the adapter is copied to `hermit/<adapter id>/`.

The score is 1 or 0. The answer is taken from the agent's final message as OpenCompass takes it: the content of the last
`\boxed{}`; without one, the sentence that says "final answer" or "answer is", else the first sentence. A judge model
reads the question, the reference and that answer and decides whether they match: every part of a multi-part answer must
be right, and an equivalent form of the reference counts. Each question runs in a fresh workspace: nothing the agent
writes in one question reaches another.
