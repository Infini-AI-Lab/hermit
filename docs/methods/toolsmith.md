# Toolsmith

Use `evolver.name: toolsmith` with `template: toolsmith@1.0` to learn instructions and callable tools together. Each
step makes one learner generation request that may rewrite `ADAPTER.md` and any number of tool bundles
(`tools/<name>/`), then self-tests the changed bundles inside the task's own sandbox and gives the learner one repair
turn with the exact failures. Choose it when the acting agent needs a capability its harness does not offer — for
example a way to look at its own output — and the task can run learner-written programs in confinement.

The acting agent never reads the learner's material: it receives `ADAPTER.md` in its context and the admitted tools as
functions. The template's two fixed exports do the exposing: `tool_catalog` lists the admitted bundles (name,
description, parameters) and the rejected ones with the reason; `tool_call` runs one bundle with the agent's workspace
as the working directory and returns its text and the images it wrote. Only the bundles change; the exports do not.

## Setup

```yaml
template: toolsmith@1.0
evolver:
  name: toolsmith
  params: {runner_tool: confine}      # a task tool with the confined runner's signature; null skips the self-tests
evolver_access:
  tools:
    - name: confine
      description: Run a program inside the task's confined sandbox.
      entrypoint: ../utils/confine.py:run_tool
      input_schema:
        type: object
        properties:
          argv: {type: array, items: {type: string}}
          cwd: {type: string}
          timeout_s: {type: number}
          ro: {type: array}
          lock: {type: [string, "null"]}
        required: [argv, cwd, timeout_s]
        additionalProperties: false
llm:
  base_url: https://your-endpoint.example/v1
  model: your-learner-model
  api_key_env: LEARNER_API_KEY
```

This fragment belongs in a complete [run config](../framework/configuration.md). The learner model is `llm`; the acting
model is configured by your pipeline. The runner is a task tool declared under `evolver_access.tools`
([task tools](../framework/task.md#interaction-with-other-components)); its entrypoint resolves against the config's
directory and the Evolver calls it as
`runner(argv=[...], cwd=..., timeout_s=..., ro=[[bundle_dir, "/tool"]], lock="shared")`.
Without a runner the Evolver still writes files, but reports every bundle as untested.

## Required integration

| Resource | Contract |
|---|---|
| Template and method | `template: toolsmith@1.0`, `evolver.name: toolsmith` |
| Confined runner | A task tool (`evolver_access.tools`) whose function takes `argv, cwd, timeout_s, ro, lock` and returns `{exit_code, stdout, stderr, seconds, timed_out, ...}`; named in `evolver.params.runner_tool` |
| Acting harness | Builds a `Session` on the adapter with resources `tool_runner` (the same runner) and `workspace` (the agent's working directory), calls `tool_catalog` once, exposes each admitted tool as a function and dispatches its calls to `tool_call(name, args)`; turns the returned image paths into image parts for the model |
| Task description | `task-info/README.md`; `task-info/environment.md` when the machine's facts (interpreters, packages, paths, limits) matter for writing a working program |
| Learner model | Any chat-completions endpoint under `llm`; the prompt is text only unless `images` is raised |

The [visual showcase recipe](../recipes/visual-showcase.md) is a complete integration:
`examples/visual_showcase/utils/confine.py` is the runner (bubblewrap, no network, the GPU visible) and
`utils/agent/agent.py` the harness that packs tool images.

## How an update works

1. **Material.** The step's task records (lowest scores first; input, output and judge feedback clipped to `max_chars`),
   the check feedback of a repaired step, a digest of each trajectory (every file operation, every command with its
   exit code and output tails, every adapter-tool call, the agent's final message; clipped to `trajectory_chars`), the
   learner's last notes and the previous self-test results.
2. **Current adapter.** `ADAPTER.md`, every file under `tools/` with line numbers, the template's `EVOLVER.md` (the
   bundle format, the execution protocol, the rules, the reply grammar), the task README and `environment.md`.
3. **One learner request.** The reply is a sequence of `<<<FILE path>>> … <<<END FILE>>>` blocks and `<<<DELETE path>>>`
   lines, then a `SUMMARY:` line. Paths are `ADAPTER.md` or `tools/<name>/<file>`; blocks with another path, a
   disallowed extension or an oversize body are skipped and reported. One retry when the reply has neither a block nor
   a summary.
4. **Apply.** Files are written; deleting a bundle's `TOOL.json` (or `<<<DELETE tools/<name>>>>`) removes the bundle.
5. **Self-tests** (when `runner_tool` is set). For up to `max_tests_per_round` bundles, changed first: the bundle's own
   `test` program, then a contract check that calls the tool exactly as the harness does (`Session.call("tool_call",
   {name, args: example_args})`) in a scratch workspace holding the best trajectory's `index.html`. A tool is *working*
   when the result is well formed and at least one listed image is a decodable, non-blank PNG inside the workspace.
   On any failure and `repair_rounds > 0`, one more learner turn with the exact failures, then a retest. A bundle that
   worked before the step and fails after the repair turn is restored (keep-last-passing).
6. **Check and record.** `check_spec` (the template's `check.py` validates every bundle without running it), the test
   results in `state/tests.json`, notes in `state/notes.jsonl`, the report and the raw replies in `output/`.

The Trainer then applies the specification checks and commits. There is no validation-based gate: a structurally valid
update that passes its self-tests can still reduce task performance. The default final selection is the current
adapter; compare checkpoints on held-out evaluation.

## Parameters and defaults

```{literalinclude} ../../hermit/algorithms/evolvers/toolsmith/evolver.yaml
:language: yaml
:start-at: params:
```

Set overrides under `evolver.params`.

## Key controls and their interactions

| Parameter | Values / default | Effect |
|---|---|---|
| `runner_tool` | A task tool name / `null` | Names the confined runner; `null` skips the self-tests and the repair turn, and every bundle is reported as untested |
| `test_timeout_s`, `test_total_s` | Seconds / 60, 300 | Bound one program and one self-test round; a bundle not reached within `test_total_s` is reported as not tested, not as failed |
| `max_tests_per_round` | Count / 4 | Bundles tested per round, changed ones first; raise it when the learner writes many tools at once |
| `repair_rounds` | Count / 1 | Learner turns spent on self-test failures; 0 keeps the first reply as written |
| `images` | Count / 0 | Self-test images attached to the learner's prompt; 0 keeps the learner text-only (it reads the image measures instead) |
| `trajectory_chars` | Characters / 20000 | Clips each trajectory digest; the digest keeps its header, the adapter-tool calls and non-zero exits before the rest |
| `max_examples`, `max_chars` | Count, characters / 8, 6000 | Bound the task records shown, as in prompt-only |
| `exposed_tools_max` | Count / 8 | What the harness exposes; the template admits at most eight bundles alphabetically and rejects the rest |

Bundles live under the template's learnable `tools/**` (text files, at most 60,000 bytes each and 40 files); the
template's `check.py` rejects a malformed bundle by naming its `TOOL.json`, so the Trainer's repair drops that bundle
alone. The template's `README.md` and `EVOLVER.md` under `hermit/algorithms/templates/toolsmith/` state the
bundle format and the execution protocol in full.

The [visual showcase recipe](../recipes/visual-showcase.md) runs this method against prompt-only and an in-process MWM
learner on the same task. Use a smoke run before increasing epochs, and evaluate with the
[same held-out protocol](../guides/evaluation.md).
