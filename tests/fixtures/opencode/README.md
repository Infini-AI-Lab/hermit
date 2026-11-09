# OpenCode streams

Recorded from OpenCode 2.2.6 (the `opencode-linux-x64.tar.gz` release binary, checked against the release's
sha256 digest) run by hermit's `opencode` Base Agent (`make("opencode").run`, isolation none), against a scripted
chat-completions model on the loopback: the model of `tests/core/test_opencode_agent.py` (`serve_chat`), bound as a
`<backend>/<model>` reference.

- `success.jsonl`: a shell call that exits 3, a call of the call's MCP tool `echo` (`hermit_tools`, offered
  directly as `hermit_tools_echo`), a write, then a reasoning text and the answer `the answer`; four requests of
  1000 prompt tokens (400 cached) and 20 completion tokens each.
- `resume.jsonl`: the same session resumed (`--session <id>`): one request.
- `failure_500.jsonl`, `failure_429.jsonl`: every request answered that status; OpenCode retries (its `retry`
  events), then ends with an `error` event (`provider.internal`, `provider.rate-limit`) and exits 1.
- `failure_refused.jsonl`: a model URL nothing listens on; the same, with `provider.transport`.
- `failure_401.jsonl`: every request answered 401; OpenCode ends at once with `provider.auth` and exits 1.

The streams are OpenCode's output unchanged: they hold no path of the machine they were recorded on.
