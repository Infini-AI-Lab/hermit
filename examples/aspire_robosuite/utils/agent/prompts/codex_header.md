# How this campaign runs your template (read first)

Below is ASPIRE's own {role_name} prompt, verbatim (NVlabs/ASPIRE, `.claude/skills/iterative-debugging/{template}`,
the part between its TEMPLATE START and TEMPLATE END markers). This campaign runs it through a harness, so a few
things in it map onto your workspace as follows. Where the template and this page disagree, this page wins.

- **Your assignment** is the block right below this page; it replaces the template's placeholder assignment.
- **Your directory.** {your_dir} Everything you write stays inside this workspace. The repository root
  (`$REPO_ROOT`) is not available to you, and you must not look for it.
- **Running episodes.** You never call `cap-harness` yourself. Wherever the template gives a `cap-harness run-batch`
  (or `run`) command, run instead, from this directory:

      bin/sim run-batch --seeds <seeds> --program <program.py> --output-root <dir> [--no-videos]
      bin/sim run --seed <seed> --program <program.py> --output-root <dir> [--no-videos]
      bin/sim validate --program <program.py>

  `bin/sim` asks the harness's simulator to run them, with the benchmark (robosuite), suite, task id, GPU, render
  settings, `--flat-run-dir` and the results file (`<dir>/results.jsonl`, with the logs under `<dir>/logs/`); the
  runs appear in your workspace. It accepts only your assigned seeds, and the unseen-check seeds once. Perception, IK and planning services are already running; you need not check them.
- **The shared skill library** is the "Shared skill library" section of your AGENTS.md, and the same files are under
  `hermit/aspire/skills/` in this workspace. Wherever the template says `.claude/skills/iterative-debugging/skills/`
  (or `skills/<file>.md`), it means these, except `skills/task-exploration.md`, which is `docs/task-exploration.md`.
  {proposed_line}
- **Documentation.** `docs/api-reference.md`, `docs/run-artifacts.md` and `docs/robosuite.md` are in this workspace;
  the scene-snapshot program is `scripts/scene_snapshot.py`.
- **Scripts the template names that you do not have** (`campaign.py`, `lint_skills.py`, `dispatch.py`,
  `run_validation.py`): skip them. For `skill_library_sha` write `"hermit"`; check `skill_report.json` by loading it
  with `python3 -m json.tool`.
- **Held-out evaluation** {heldout_line}
- **Finish** {finish_line}
