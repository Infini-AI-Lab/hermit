# Pi streams

Recorded from Pi 1.5.0 (the `pi-linux-x64.tar.gz` release binary) run by hermit's `pi` Base Agent
(`make("pi").run`, isolation none), against a scripted chat-completions model on the loopback: the model of
`tests/core/test_pi_agent.py` (`serve_chat`), bound as a `<backend>/<model>` reference.

- `success.jsonl`: a bash call that exits 3, a call of the call's MCP tool `echo` (`hermit_tools`), a write, then the
  answer `the answer`; four requests of 1000 prompt tokens (400 cached) and 20 output tokens each.
- `resume.jsonl`: the same session resumed (`--session <file>`): one request.
- `failure.jsonl`: every request answered 503; Pi retries three times, reports `stopReason: error` and exits 0.

The streams are Pi's output unchanged but for two paths: the run's directory is `/run` and Pi's installation
`/opt/pi`.
