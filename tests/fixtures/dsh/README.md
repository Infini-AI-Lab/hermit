# DeepSeek Harness streams

Recorded from DeepSeek Harness 0.2.2-rc.1 (`npm install -g @deepseek-ai/dsh@0.2.2-rc.1`, on Node 24.22.2, Linux) run
by hermit's `dsh` Base Agent (`make("dsh").run`, isolation none), against a scripted chat-completions model on the
loopback, bound as a `<backend>/<model>` reference with `effort: high`; the model streams a `reasoning_content`
delta before each answer when it is asked for a reasoning effort.

- `success.jsonl`: a bash call that exits 3, a bash call that exits 0, a call of the call's MCP tool `echo`
  (`hermit_tools`), a write, an edit, then the answer `the answer`; six requests of 1000 prompt tokens (400 cached)
  and 20 output tokens each.
- `resume.jsonl`: the same session resumed (`--session-id`): one request.
- `failure.jsonl`: every request answered 503; dsh retries five times, ends the run with the error `SERVER`, writes
  `dsh: SERVER: ...` on stderr (the last line, which the Base Agent keeps in the stream) and exits 1.
- `promoted.jsonl`: bash `echo building; sleep 5` and `sleep 5` with `timeoutMs: 1000`, and the first again with
  `timeoutMs: 1000.5`, then the answer. The Base Agent's layer leaves the `tool-bash` row's `promoteOnTimeout` at
  dsh's default, true, so at its timeout dsh moves each command to a background job: the result is the output so
  far, then `[still running after <timeoutMs>ms; moved to background job <id>]` (the marker alone for `sleep 5`,
  which printed nothing), with no exit marker.

Recorded by running dsh directly (`dsh --profile headless --json --permission-mode danger-full-access`, with a patch
layer like the Base Agent's) against a scripted chat-completions model:

- `bash.jsonl`: how dsh's bash tool marks a command's end. The model calls bash nine times: `exit 3`, `printf ''`,
  `true`, `echo out; exit 4`, `kill -9 $$`, `printf 'no newline'; exit 5`, then with `timeoutMs: 1000`
  `trap 'exit 0' TERM; sleep 5 & wait` (stopped by its timeout, it exits 0) and `sleep 5` (killed), and the first of
  these again with `timeoutMs: 1000.5` (dsh prints the timeout it applied, here the model's), then answers `done`.
  The layer also sets the `tool-bash` row's `promoteOnTimeout: false`, so a timeout stops the command rather than
  moving it to a background job.
- `max_tokens.jsonl`: the model's answer ends at its token limit (`finish_reason: length`): the run ends `max-tokens`
  and dsh exits 1, writing nothing on stderr.
- `big_bash.jsonl`: bash prints 1,500 lines (about 42 KB) and exits 2; dsh's `--json` keeps 8 KiB of the result and
  marks the event `truncated`, so the exit marker is cut off.
- `background.jsonl`: bash with `run_in_background: true` returns `started background job <id>`, no exit marker.
- `fake_marker.jsonl`: bash prints `built` and a line `[exit code: 7]` of its own, then exits 0.
- `no_usage.jsonl`: the model sends no usage; dsh reports the step's usage as zeros.
- `tools_down.jsonl`: the session's MCP row (`failOnStartupError: true`) points at a port nothing listens on: dsh
  writes `warning: 1 entry did not activate` and the row's error on stderr, then completes the run and exits 0.
- `bad_option.jsonl`: an unknown option after `--json`: one `error` event, no session, exit 1.
- `dotenv.jsonl`: the working directory's `.env` sets `PYTHONPATH`: dsh stops before it writes an event (its
  error on stderr) and exits 1.
- `stopped.jsonl`: the model calls bash `sleep 60`; two seconds after dsh writes the call, it gets SIGTERM (as a
  session's timeout or the Trainer's stop sends it): no `final` event, an `error` event and its stderr line
  (`flush on a closed handle`), exit 0. (With a task's data bound in the workspace, the binding's bubblewrap is what
  the signal ends, and dsh with it: no event at all.)
- `context_window.jsonl`: the route declares no `contextWindow`, and the server refuses a request whose prompt and
  output tokens pass its model's 32,768, with vLLM's error: dsh asks for 128,000 output tokens, then 65,536, and ends
  the run `CONTEXT_WINDOW_EXCEEDED` (exit 1). With `contextWindow: 32768` declared it asked for 22,503 and completed.
- `retried.jsonl`: the server answers the first request 503, then the retry with 1000 prompt tokens (900 of them
  cached) and 10 output tokens: dsh sums the two attempts' usage into the step's, and leaves out the cache count the
  refused attempt did not report (its zeros), so the step reports `inputTokens` 100 and `totalTokens` 1010.

The streams are dsh's stdout unchanged, followed by its stderr's lines where it wrote any, as the Base Agent keeps
them in one stream; the run's directory was `/run` (`/run/<name>` for the streams recorded directly, `/tmp/rec3` for
`stopped.jsonl`, `/tmp/r4` for `context_window.jsonl`, `/tmp/r5` for `retried.jsonl`).
