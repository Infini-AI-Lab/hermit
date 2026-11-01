"""The agent stack: every agent in Hermit is built from three layers (docs/framework/agent-layers.md).

    model/     Layer 1: a model and how to reach it
    base/      Layer 2: Base Agents (direct, react, codex, claude) behind one API
    acting/    Layer 3: Acting Agents, which answer tasks
    evolver/   Layer 3: Evolver Agents, which learn adapters (methods/: the learning methods)

Imports go one way: Layer 3 imports Layer 2, Layer 2 imports Layer 1.
"""
