"""Harbor benchmarks in Hermit: one acting agent for every task set in Harbor's format.

    agent:
      name: harbor
      params:
        tasks_dir: ../utils/tasks            # the Harbor task directories (task.toml, environment/, tests/)
        model: gpt-5.6-luna                  # the acting model: a plain name, or a `<backend>/<model>` reference
        acting_agent: {name: codex}          # Harbor's agent in the container (built-in name or import path)
        agent_timeout_sec: 3600              # uniform agent budget; null: each task's own
        jobs_dir: ~/.cache/hermit/harbor/jobs

The runner (`runner.HarborAgent`) turns one Hermit rollout into one Harbor trial: Harbor starts the task's
container, installs the acting agent, uploads the deployed adapter, runs the instruction, then the task's own
verifier, and the trial's reward comes back as the rollout's output; `judge.score` reads it. A task with
`[[steps]]` (a stream: one environment that lives through many steps, each with its instruction and verifier) is
run one step per rollout by a driver (`harborside/driver.py`) that keeps the environment between the Trainer's
steps, so a learner acts between them.

`dataset` turns a task set into `tasks.jsonl` rows, splits and a content lock; `staging` deploys the adapter for the
container; `errors`, `curate` and `docker` are the runner's parts. `harborside/` is the half that runs under
Harbor's own interpreter (harbor and the standard library only): the container-side agents and the stream driver.
"""
