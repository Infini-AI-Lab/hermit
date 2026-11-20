# Benchmark checklist

Answer these items alongside the [general checklist](general-pr-checklist.md) when a pull request adds a benchmark
or changes how a benchmark's tasks are written or read. The contract is in
[Task directories](../../../../docs/framework/task.md#task-directories) and
[Benchmarks](../../../../docs/framework/task.md#benchmarks); how a task runs in Harbor is in
[How Harbor runs a task](../../../../docs/developer/harbor-design.md).

- [ ] **Task directories.** Are the tasks task directories in Harbor's format, written by the benchmark's
  `prepare.py` with `hermit.task.directory.write_task` (or taken from a Harbor dataset as they are), rather than a new
  row format? A task set whose tasks each need an image of their own runs in Harbor's environments with
  `hermit.task.verify`, never a runner of its own.
- [ ] **Public and private halves.** Do the rows the agent and the learner read hold only the public half (the
  instruction, its settings, its metadata), with answers, tests and the verifier in the private half that only the
  judge reads? Name each field a `data.view` adds and why the learner may see it.
- [ ] **What a task asks.** Does each task's `task.toml` declare the network (`network_mode`, `allowed_hosts`),
  memory and timeouts its agent and its verifier need, so that a run unable to give them refuses the task rather than
  running it with less?
- [ ] **Parity, for a moved benchmark.** For a benchmark moved from rows to task directories, do its tasks read back
  through each of its configs as exactly the old rows (prompts, learning material, splits)? State how this was
  compared, with the counts.
- [ ] **A real run.** Link one smoke run through `hermit eval` (and `hermit train` when a method is part of the
  change), with its scores and no infrastructure errors.
