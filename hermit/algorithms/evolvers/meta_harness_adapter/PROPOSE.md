You are the proposer in round {round} of a search over adapters. Each candidate adapter is evaluated on the same
search tasks; the search returns the candidate with the best mean score there.

{message}

What you have:
- {adapter}/.hermit/EVOLVER.md: what an adapter is.
- {task_info}/README.md: the task, and how the acting agent receives the adapter.
- {summary}: every candidate so far, one line each: its id, round, hypothesis, the candidate it was based on, its mean
  score, and where its files and its rollouts are.
- {store}/<id>/adapter/: each candidate's files.
- {learning_material}/step_NNNN/: the rollouts of one candidate on the search tasks: for each task its input, the
  agent's output, the judge's score and feedback, and the full trajectory ({learning_material}/README.md describes
  the layout).

What you deliver:
- Exactly {k} new candidates, each a complete adapter in its own directory {candidates_dir}/<name>/ (name: lowercase
  letters, digits, '_' and '-'). A candidate may start from a copy of any earlier one, made in
  {candidates_dir}/<name>/ first, or from nothing; never change the files under {store}. It must not depend on files
  outside its own directory. Its `.hermit/` is replaced with the template's.
- Call `check_spec` with each candidate's path and fix what it reports.
- {pending}: {"candidates": [{"name": "<name>", "hypothesis": "<a falsifiable claim about what the candidate
  changes and why it should score higher>", "base": "<the id of the candidate it builds on, or null>"}]}

Rules:
- Do not write facts about individual search tasks or their answers into a candidate: a candidate must be as useful
  on unseen tasks of the same kind.
- Deliver all {k} candidates; do not stop early or conclude that the search is finished.
- You do not run the evaluation; it runs after you finish.
