# hier_search Evolver

A prompt-and-configuration Evolver for the `hier_search` architecture. Its
objective is fixed: the acting model must keep answering at least as many
questions correctly, at lower cost (fewer main-model tokens and search rounds).
It reads per-question trajectories and per-batch totals, makes one supported
change to the adapter (reader prompt, reporting, what the acting model is
told, tool descriptions, implementation), and verifies the candidate with the
session's `try_tool` and `small_model` capabilities, which reach the bound
corpus service and reader model through the broker while the learner's own
commands stay offline.

Usage:

    harnessadapter evolve <adapter> --evolver hier_search \
      --learning-material <dir> --backend codex --model gpt-5.6-sol --reasoning-effort high \
      --bind search=http://127.0.0.1:8012 --bind documents=http://127.0.0.1:8012 \
      --bind small_model='http://127.0.0.1:8011/v1|Qwen/Qwen3.5-4B' \
      --output <dir outside the framework>
