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

Recorded by running dsh directly (`dsh --profile headless --json --permission-mode danger-full-access`, with a patch
layer like the Base Agent's) against a scripted chat-completions model:

- `bash.jsonl`: how dsh's bash tool marks a command's end. The model calls bash eight times: `exit 3`, `printf ''`,
  `true`, `echo out; exit 4`, `kill -9 $$`, `printf 'no newline'; exit 5`, then with `timeoutMs: 1000`
  `trap 'exit 0' TERM; sleep 5 & wait` (stopped by its timeout, it exits 0) and `sleep 5` (killed), then answers
  `done`. The layer also sets the `tool-bash` row's `promoteOnTimeout: false`, so a timeout stops the command rather
  than moving it to a background job.
- `max_tokens.jsonl`: the model's answer ends at its token limit (`finish_reason: length`): the run ends `max-tokens`
  and dsh exits 1, writing nothing on stderr.

The streams are dsh's output unchanged; the run's directory was `/run` (`/run/bash` and `/run/max_tokens` for the
last two).
