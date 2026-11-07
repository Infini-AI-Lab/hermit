# Event streams of the Claude Code CLI

Real `claude -p --output-format stream-json --verbose` output of Claude Code 2.1.318, each run in a loopback-only
namespace on a scripted local model with a fake token (no network, no subscription, no cost): one file per case, its exit
code and what the model's side did in `index.json`. The run's directory is replaced by `/probe`, the `init` event is
cut down to the fields a reader uses, and nothing else is changed. `tests/core/test_claude_subscription.py` replays them
through the shared entry points with a stand-in `claude`, so that a reader is held to what the CLI prints.
`text_session.jsonl` is the reference for the `init` of a session that only replies (no tools, no MCP servers): the
stand-in gives any stream that `init` when it is given `--restricted`, as the CLI does.

What these streams cannot say: what the real service sends at a real limit (the limit cases are the CLI's reaction to the
`anthropic-ratelimit-unified-*` headers named in its binary). See `docs/guides/claude-subscription.md` for what
was run on a real subscription.
