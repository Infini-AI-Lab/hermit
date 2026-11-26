# MATH-500

Competition mathematics problems from MATH (Hendrycks et al.), across seven subjects (prealgebra, algebra, number
theory, counting and probability, geometry, intermediate algebra, precalculus) and five difficulty levels. The test
problems are MATH-500, OpenAI's subset of MATH's test split; the training and validation problems are drawn from MATH's
training split. An answer is a number or an expression: a fraction, a radical, an interval, a coordinate pair, a
polynomial, a word. The agent is given MATH-500's own prompt (think step by step, end with "Therefore, the final answer
is: $\boxed{ANSWER}$. I hope it is correct") followed by the problem, and has half an hour.

The agent is a coding CLI (Codex or Claude Code, as the run's config names) in a sandbox of its own, with a shell and
Python (the packages of the run's Python environment) and no network but its model's; web search is off. Its loop and
its tools are fixed; the adapter is what changes. The adapter is deployed into the working directory as `hermit
deploy` lays it out for that CLI: `ADAPTER.md` is the agent's instructions (a block of `AGENTS.md` for Codex,
`CLAUDE.md` for Claude Code), and the rest of the adapter is copied to `hermit/<adapter id>/`.

The score is 1 when the content of the last `\boxed{}` in the agent's final message equals the reference, else 0; a
reply without a `\boxed{}` scores 0. Equality is Math-Verify's: numbers, fractions, intervals, sets, matrices and
expressions are compared as values (`0.5` and `\frac{1}{2}` are the same answer), text as text. Each problem runs in a
fresh workspace: nothing the agent writes in one problem reaches another.
