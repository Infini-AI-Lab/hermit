# Codex streams

Streams in the shapes `codex exec --json` of Codex 0.164.0 (the standalone release, Linux) writes, its stderr merged
in as Hermit's runs and harborside keep it: written from a recorded trial's stream with its ids and times replaced and
its repeated lines cut, every message as Codex wrote it.

- `unreachable.jsonl`: a session on a ChatGPT login whose requests to chatgpt.com all failed (a TB2.0 trial, Codex
  0.164.0 on gpt-5.6-luna, 2026-11-08). Codex's refresh of its models and its MCP client failed ("error sending
  request"); it reconnected over WebSockets ("Reconnecting... 5/5 (workspace routing discovery failed)"), fell back
  to HTTPS (an `error` item), reconnected again, then reported the error and failed the turn with it ("workspace
  routing discovery failed"), and exited 1. Read by `core/test_base_agents.py`, `core/test_cli_agent.py` (the
  stand-in `codex` prints it) and `core/test_harbor_runner.py` (a trial's stream).
