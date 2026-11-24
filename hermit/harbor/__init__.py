"""Benchmarks in Harbor's format: the task sets Harbor's registry holds (Terminal-Bench, SWE-bench, ...), made into an
example's data, and an eval's figures read from its results.

    python -m hermit.harbor.dataset prepare utils/benchmark.yaml    # the download, tasks.jsonl, splits, a lock
    python -m hermit.harbor.report runs/<run>/eval/<adapter>        # pass@1 over the runs that are the agent's result

Harbor itself gives each task its environment and its verifier (hermit.environment.harbor, `environment.harbor` in a
run config); the agent that works in it is Hermit's, any acting agent (docs/framework/task.md#benchmarks).
"""
