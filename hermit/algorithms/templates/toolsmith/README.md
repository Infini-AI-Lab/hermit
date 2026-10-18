# Toolsmith template

An adapter that learns two things for an acting agent: instructions (`ADAPTER.md`, placed in the agent's context) and
tools (`tools/<name>/` program bundles that the agent's harness exposes as callable tools, whose results may carry
images). The learner writes ordinary programs — Python, Node or bash — and a one-file `TOOL.json` description; the
template's fixed code lists them and runs them inside the task's confined sandbox. Nothing executes on the bare host.

```text
ADAPTER.md              Learned instructions (UTF-8, ≤ 40,000 bytes), shown to the agent verbatim
tools/<name>/TOOL.json  One bundle per tool: name, description, parameters, entry, optional test, example_args, timeout_s
tools/<name>/run.*      The program (.py, .cjs, .js, .mjs or .sh); JSON arguments on stdin, a JSON result line on stdout
tools/README.md         The bundle format with a complete example (fixed)
interface.py            The exports: tool_catalog (what is admitted, what was rejected and why) and tool_call (run one)
check.py                The template check: every bundle is well-formed; never runs a program
EVOLVER.md              The learner's contract: formats, the execution protocol, the rules, the reply grammar (fixed)
adapter.yaml            The manifest: exports, structure (learnable / fixed), check
```

How a host uses it: build a `Session` on the adapter with two resources, `tool_runner` (a callable with the confined
runner's keyword signature `argv, cwd, timeout_s, ro, lock`) and `workspace` (the agent's working directory), call
`tool_catalog` once and expose each admitted tool to the model, then dispatch its calls to
`tool_call(name, args)`. `tool_call` binds the bundle read-only at `/tool`, runs the entry with the workspace as the
working directory and the arguments on stdin, reads the last JSON line of stdout and validates the images it names
(inside the workspace, PNG, ≤ 2 MB, no symlinks). Without a bound runner it raises instead of running.

The check (`check.py`) admits a bundle only when its name matches the directory and the slug rule, is not a primitive's
name, every parameter is optional, the entry exists, the files are text of the allowed kinds, and no file carries an
HTML page. At most eight tools are listed; the rest are reported as rejected. A bad bundle fails check 6 with its
`TOOL.json` as the path, so the Trainer's repair drops that bundle alone.

Train it with `template: toolsmith@1.0` and `evolver: {name: toolsmith, params: {runner_tool: <a task tool that runs
inside the sandbox>}}`; see `docs/methods/toolsmith.md` and the visual showcase recipe. Image measures for the learner's
self-tests come from `hermit.runtime.pngstats` (standard library only).
