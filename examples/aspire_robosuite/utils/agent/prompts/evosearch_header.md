# Part 2: the evolutionary search (only when the fix loop stays below 90%)

When Part 1 is done, compare `fix_code.py`'s development pass rate with 90%. At or above it, you are finished; give
Part 1's return message. Below it, run ASPIRE's evolutionary-search subagent on this task, which follows verbatim
(NVlabs/ASPIRE, `.claude/skills/evosearch/subagent-prompt.md`), and return its message too. It maps onto this
workspace as follows; where it and this page disagree, this page wins.

- **Assignment.** `BENCHMARK` {benchmark}, `SUITE` {suite}, `TASK_ID` {task_id}, `TASKSHORT` {suite},
  `BASELINE_RATE` `fix_code.py`'s development rate from Part 1, `EVOSEARCH_DIR` `evosearch`, so
  `TASK_DIR` is `{evo_dir}` in this workspace. The fix loop's `fix_code.py` is `{task_dir}/fix_code.py`: seed it as
  candidate_A verbatim.
- **Seeds.** Wherever the template says 51–65 (the debug set), use your development seeds, **{dev}**. Wherever it
  says 1–50 (the held-out set), they are not yours: never run them.
- **Running candidates.** There is no `evosearch_eval.py` or `analyze_evosearch_traces.py`. Evaluate each candidate
  with `bin/sim run-batch --seeds {dev} --program <iter>/candidate_X/code.py --output-root <iter>/candidate_X/eval
  --no-videos`, read each `results.jsonl` with the outcomes of its runs, and write `<iter>/iter_summary.json` as
  `{{"candidates": [{{"candidate": "candidate_X", "pass_rate": <passes / seeds>}}, ...]}}`. Read the traces and
  keyframes of the failing runs yourself (`docs/run-artifacts.md`). The scene snapshot is `scripts/scene_snapshot.py`
  through `bin/sim run`. The skill library and `skills/evosearch-iteration.md` are `hermit/aspire/skills/` and
  `docs/evosearch-iteration.md`.
- **Step 7.** Do 7a (save `evosearch_best_code.py`, falling back to `fix_code.py` when the search did not beat it)
  and 7c (its `findings.md`, in `{evo_dir}`). Skip 7b and `outputs/working_codes`: the harness runs Stage 2 on
  `evosearch_best_code.py` after you finish.
