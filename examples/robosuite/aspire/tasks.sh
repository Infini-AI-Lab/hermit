# Sourced by the engine's launcher (../../cap_harness/launch.sh) in this folder, after the benchmark's prepare.py:
# ASPIRE's verifiers beside Robosuite's tasks (hermit/agent/evolver/methods/aspire/rows.py, on ASPIRE's first five
# development seeds), in tasks/ and splits/ here, which this folder's configs read.
"$HERMIT" -m hermit.agent.evolver.methods.aspire.rows --benchmark ../utils/benchmark --out . --verify-seeds 101-105 \
  --engine ../../cap_harness/utils/benchmark/tasks.py
TASKS="$PWD/tasks"
