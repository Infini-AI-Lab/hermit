# Continual Harness (Pokemon Red)

An LLM agent plays Pokemon Red through the [Continual Harness](https://github.com/sethkarten/continual-harness)
environment (mGBA/PyBoy emulator + game server). Each task is one *window*: the agent plays a fixed number of steps
with the current policy (the adapter's `ADAPTER.md`), and the score is the benchmark's milestone progress.

Hermit owns the agent and evolves the policy; the Continual Harness repo owns the environment and the scoring.
Set `HERMIT_CH_DIR` to the checkout and `FIREWORKS_API_KEY` for the model.
