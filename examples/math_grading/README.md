# Math grading

The grading the math benchmarks share ([AIME](../aime/README.md), and the others under `examples/` that name it): not a
task of its own, the engine their configs and judges point at.

| File | What it does | Where it runs |
|---|---|---|
| `utils/grade.py` | `answer`: the CLI agent's output, the last complete `\boxed{}` of its final message (MathArena's rule) and the reply's tail; `score`: 1 when that answer equals the reference, else 0, the feedback naming the reference | `answer` in the task sandbox (the configs grant `utils/`; standard library only); `score` on the host, from a benchmark's `utils/benchmark/judge.py` |
| `utils/math_compare.py` | one comparison by Math-Verify, read as LaTeX only (its other extractor evaluates Python through sympy) | a process of its own, started by `grade.equal` with a 60 s limit and, on Linux, a 2 GB memory bound: an answer whose arithmetic never ends scores 0 and holds no other judge |
| `requirements.txt` | Math-Verify, and pyarrow for the benchmarks' `prepare.py` | the host |
| `utils/tests/` | the grader's tests (they need Math-Verify) | |

A change here runs the test groups of every benchmark that uses it (`ENGINES` in `tests/selection.py`).
