"""Benchmarks in Hermit: one acting agent for every task set in Harbor's format, the protocol a benchmark enters by.

    agent:
      name: harbor
      params:
        tasks_dir: ../utils/tasks            # the Harbor task directories (task.toml, environment/, tests/)
        acting_agent: {cli: codex}           # the CLI in the container: codex, claude, pi, opencode, dsh,
                                             # mini-swe-agent, or answer (one model call); or a Harbor built-in by name
        model: fireworks/glm-5               # a `<backend>/<model>` reference, or with `login` the CLI's model name
        web_search: false                    # the CLI's web search, always set
        agent_timeout_sec: 3600              # uniform agent budget; null: each task's own
        jobs_dir: ~/.cache/hermit/harbor/jobs

The runner (`runner.HarborAgent`) turns one Hermit rollout into one Harbor trial. Hermit renders the trial and
harborside carries it out: the adapter is deployed for the acting CLI (`staging`, by the CLI's own host, with the
task's context when the adapter's is per task: `context`), its tools are served from this machine (`tools`), and the
CLI's launch plan is rendered by Hermit's own CLI Base Agent (`launch`: the CLI's home, command and environment, with
`clis`, the table of what differs per CLI). Harbor then starts the task's container and the HermitXXX class runs the
plan in it (harborside/hermit.py), then the task's own verifier; the trial's reward and the CLI's own stream come back,
read as Hermit reads its own sessions (`errors` for the trial's verdict, the Base Agent's `outcome` and `parse` for
the session's), and `judge.score` scores it. A task with `[[steps]]` (a stream: one environment that lives through
many steps, each with its instruction and verifier) is run one step per rollout by a driver (`stream`,
`harborside/driver.py`) that keeps the environment between the Trainer's steps, so a learner acts between them.

`dataset` makes a benchmark's data from its benchmark file (`python -m hermit.harbor.dataset prepare`: the download,
`tasks.jsonl` rows, splits, a content lock); `curate` and `docker` are the runner's parts. `harborside/` is the half
that runs under Harbor's own interpreter (harbor, two of Harbor's own dependencies, pydantic and certifi where it is
installed, and the standard library only): the container-side agents and the stream driver.
"""
