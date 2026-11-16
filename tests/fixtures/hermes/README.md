# Hermes Agent streams

Recorded from Hermes Agent 0.19.0 (`uv tool install 'hermes-agent[mcp]==0.19.0'`, Python 3.12, Linux) run by
hermit's `hermes` Base Agent (`make("hermes").run`, isolation none) against a scripted chat-completions model on the
loopback, bound as a `<backend>/<model>` reference, with the call's MCP tool `echo` (`hermit_tools`). Each line is an
event of hermit's plugin (hermit/agent/base/hermes_plugin), which the session's stdout carries, then Hermes's own
lines (its stderr's `session_id: ...`, the reply `chat -Q` prints). Every request answers 1000 prompt tokens (400
cached) and 20 output tokens. The workspace's path is written `/w`, the model's URL `http://127.0.0.1:9/v1`.

- `success.jsonl` (`effort: high`, so the model streams a `reasoning_content` delta before each answer): `terminal`
  `echo out; exit 3`, `terminal` `echo fine`, the MCP tool `echo`, `write_file` `note.txt`, `patch` `a.txt`, then the
  answer `the answer`: six requests.
- `resume.jsonl`: the same session resumed (`--resume`): one request, no `hermes.session_start`.
- `unavailable.jsonl`: every request answered 503; Hermes classes it `overloaded`, tries three times and exits 1 with
  `API call failed after 3 retries: ...` on stdout.
- `rejected.jsonl`: the request answered 400; Hermes classes it `format_error`, does not retry and exits 1.
- `turn_limit.jsonl` (`max_turns: 2`): two `terminal` calls, then Hermes's summary request (not streamed, no hook:
  no `hermes.api_request`), whose answer and Hermes's own note are the reply; the turn ends `completed: false` and
  Hermes exits 0.
- `interrupted.jsonl` (`timeout_s: 8`): `terminal` `sleep 60`, which the Base Agent's stop (SIGTERM) interrupts: the
  tool's result is Hermes's cancellation, the turn ends `interrupted: true`, Hermes exits 130.

Recorded by running `hermes chat -Q` directly in a home the Base Agent prepared (`Hermes.prepare_home`), against the
same kind of scripted model (another usage: 1234 prompt tokens, 1000 cached, 56 output):

- `tools_down.jsonl`: the home's MCP server is a URL nothing serves (`mcp_discovery_timeout: 5`): the first
  request's tools (`hermes.request_tools`) leave out `hermit_tools`, and Hermes runs on without them: a terminal
  call, then the answer; it exits 0.
