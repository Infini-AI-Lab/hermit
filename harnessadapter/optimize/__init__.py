"""Optimizer Evolvers: search-based adapter optimization against an Environment.

- environment: the Task / Rollout / Environment evaluation contract
- surface:     an adapter's optimizable text files, and materialized candidates
- llm:         the optimizer-side model (OpenAI-compatible chat, with a ledger)
- plugin:      the optimizer plugin contract and registry (evolver.toml kind = "optimizer")
- session:     one optimize session (candidate + Training State + record), and
               evaluate_adapters for the held-out comparison
- plugins/:    GEPA and SkillOpt, each running its upstream library unchanged
- testing:     a $0 environment and a local fake model server
"""
from harnessadapter.optimize.environment import Environment, Rollout, Task
from harnessadapter.optimize.session import evaluate_adapters, optimize

__all__ = ["Environment", "Rollout", "Task", "evaluate_adapters", "optimize"]
