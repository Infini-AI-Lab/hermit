# Toolsmith adapter: the learner's contract

This document is for the learner (the Evolver's model), not for the acting agent. It states what the adapter is made
of, how a tool is written and run, the rules a change must follow, and the exact form of a reply.

## What you maintain

1. `ADAPTER.md` — learned instructions. The acting agent receives this text verbatim in its context before every task.
   UTF-8, at most 40,000 bytes. Write what the agent should do and in which order; name the tools it should call and
   when. The agent never sees this guide, the tool sources or the task records: ADAPTER.md is your only channel of
   instructions to it.
2. `tools/<name>/` — learned tools. Each directory with a `TOOL.json` is one tool. The agent's harness lists every
   admitted tool, with its description and parameters, as a function the agent can call next to its own primitives
   (write_file, read_file, replace_text, run_command). The tool runs inside the task's sandbox and answers with text
   and, optionally, images that are shown to the agent.

Everything else (`interface.py`, `check.py`, `README.md`, `tools/README.md`, `adapter.yaml`, this guide) is fixed.

## The tool bundle

A bundle is a directory `tools/<name>/` holding `TOOL.json` and the program it names. `TOOL.json`:

```json
{
  "name": "<the directory name>",
  "description": "<what it does, when the agent should call it; at most 1024 characters>",
  "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": false},
  "entry": "run.py",
  "test": "test.py",
  "example_args": {},
  "timeout_s": 60
}
```

- `name`: equals the directory name; matches `^[a-z][a-z0-9_]{0,63}$`; not one of the reserved names write_file,
  read_file, replace_text, run_command, inspect_page, wm, tool_catalog, tool_call, context_package.
- `description`: a non-empty string of at most 1024 characters. This is what the agent reads to decide whether to call
  the tool: say what it does, what it returns, and when to use it.
- `parameters`: a JSON Schema with `"type": "object"`. Allowed keywords: type, properties, required,
  additionalProperties, description, enum, default, items, minimum, maximum. `required` must be `[]`: every parameter
  is optional, so the tool must work when called with no arguments at all (`{}`). Give each parameter a default in the
  program.
- `entry`: the program file, in the bundle directory: `run.py`, `run.cjs`, `run.js`, `run.mjs` or `run.sh`.
- `test` (optional): a self-test program, `test.py`, `test.cjs` or `test.sh`, run with no arguments; exit code 0 means
  pass. It runs in a scratch working directory that holds an `index.html`; it may spawn the entry itself.
- `example_args` (optional, default `{}`): arguments that fit `parameters`; the self-test of the contract calls the tool
  with them.
- `timeout_s` (optional, default 60): an integer from 1 to 90.

Other files in the bundle may only be `.py .md .json .txt .js .cjs .mjs .sh`, UTF-8 text, at most 60,000 bytes each; no
file or directory name may start with a dot; at most 40 files under `tools/` in total. Directories whose name starts
with `_` or `.` and directories without `TOOL.json` are ignored. At most 8 tools are admitted (alphabetically); more
are rejected as "too many tools".

A bundle that breaks a rule is rejected (the agent does not see it) and, at the end of the step, removed by the
Trainer's repair; you read the reason in the next step's check feedback and in the catalog's `rejected` list.

## How a tool runs

The same protocol serves the agent's calls and the self-tests:

- The interpreter follows the extension: `.py` → `python3 -I -B`, `.cjs` `.js` `.mjs` → `node`, `.sh` → `bash`.
  These are the task's interpreters as its environment facts describe them: the venv Python 3.12 (standard library,
  PyYAML and jsonschema; no playwright, no PIL), Node 22, bash. There is no network.
- The bundle directory is mounted read-only at `/tool`; the program is started as `/tool/<entry>`. Because Python runs
  with `-I`, sibling imports are not available: write one self-contained entry file (a Node entry may `require()` a
  sibling by its path under `/tool`).
- The working directory is the agent's workspace (read-write); it holds the agent's `index.html` and `README.md`.
  Write every output under the working directory, with relative paths. `/tmp` is private to the call and discarded
  afterwards; `HOME` is `/tmp/home`.
- The arguments arrive on stdin as one JSON object (`{}` when the agent passed none). Read all of stdin; treat an empty
  stdin as `{}`.
- The result is the LAST line of stdout that parses as a JSON object: `{"text": "<what the agent reads>",
  "images": ["<path>", ...]}`. `text` is required (keep it under 12,000 characters; it is clipped beyond that).
  `images` is optional: PNG files (only PNG) the tool wrote inside the working directory, absolute or relative to it,
  regular files reached without symbolic links, at most 2 MB each. The first 5 are shown to the agent as images in
  the message that follows the call; the rest are dropped. A missing file, a symlink or a file outside the working
  directory is reported, not shown.
- Exit code 0 means success. A non-zero exit, a timeout (`timeout_s`), or no JSON object line on stdout is reported to
  the agent as an error with the tails of stdout and stderr. Keep the total stdout small (a few thousand characters):
  only the head and the tail of a long stdout are kept, and the result line must survive.
- Each call of a tool is independent: no state is kept between calls except the files the tool writes in the working
  directory.

## How the agent's run ends

- A tool's images reach the agent from its next model request on (they stay until a later image-bearing call replaces
  them). Images returned during the agent's last request never reach it. One model request may hold several tool calls;
  the task's budgets (requests, tool calls, adapter-tool calls, time) are counted separately, as the task states them.
- When the run ends, the harness calls the adapter's fixed `finalize` export: if the agent changed its work after the
  last render of it that reached the agent, the last version the agent saw is submitted instead of the final one.
  A render counts as seen when at least one of its images is non-blank and it showed the work itself (for this task:
  the workspace's `index.html` at the time of the call, not another file named in the arguments). Changes made after
  the agent's last look are discarded, so a run should end on a look of the version it wants submitted.

## A complete example (non-visual)

`tools/wordcount/TOOL.json`:

```json
{
  "name": "wordcount",
  "description": "Report the size of a file in the working directory as bytes and lines. Default file: index.html.",
  "parameters": {
    "type": "object",
    "properties": {"file": {"type": "string", "description": "File to measure; default index.html"}},
    "required": [],
    "additionalProperties": false
  },
  "entry": "run.py",
  "test": "test.py",
  "example_args": {},
  "timeout_s": 10
}
```

`tools/wordcount/run.py`:

```python
import json
import sys

raw = sys.stdin.read()
args = json.loads(raw) if raw.strip() else {}
name = args.get("file", "index.html")
try:
    data = open(name, "rb").read()
except OSError as exc:
    print(json.dumps({"text": f"cannot read {name}: {exc}"}))
    sys.exit(0)
lines = data.count(b"\n")
print(json.dumps({"text": f"{name}: {len(data)} bytes, {lines} lines"}))
```

The same tool as `tools/wordcount/run.cjs` (then `"entry": "run.cjs"` in TOOL.json):

```js
const fs = require("fs");
const raw = fs.readFileSync(0, "utf8");
const args = raw.trim() ? JSON.parse(raw) : {};
const name = args.file || "index.html";
let data;
try {
  data = fs.readFileSync(name);
} catch (err) {
  console.log(JSON.stringify({text: `cannot read ${name}: ${err.message}`}));
  process.exit(0);
}
let lines = 0;
for (const byte of data) if (byte === 10) lines += 1;
console.log(JSON.stringify({text: `${name}: ${data.length} bytes, ${lines} lines`}));
```

`tools/wordcount/test.py` (optional; exit 0 = pass):

```python
import json
import subprocess
import sys

open("wordcount_test.txt", "w").write("one\ntwo\n")
run = subprocess.run([sys.executable, "-I", "-B", "/tool/run.py"], input=b'{"file": "wordcount_test.txt"}',
                     capture_output=True)
assert run.returncode == 0, run.stderr
result = json.loads(run.stdout.decode().strip().splitlines()[-1])
assert result["text"] == "wordcount_test.txt: 8 bytes, 2 lines", result
```

A tool that produces an image writes the file under the working directory and lists it:
`print(json.dumps({"text": "rendered index.html at 1280x800", "images": ["shots/desktop.png"]}))`.

## Rules

- Every tool must work with no arguments.
- Images listed in the result are shown to the agent (at most 5 per call). Images are the only way the agent sees what a
  page looks like; text alone cannot carry that.
- Only include the files you change; a file block replaces the whole file.
- Keep what works, and extend a tool when the evidence shows a gap: the host's checks found a problem that the tool's
  report on the same version did not show; the host observes a view, size or moment that the tool never renders; or a
  probe page fails. A tool that worked before and fails its self-test after a change is restored from its last
  passing version, so a failed improvement costs nothing.
- Never copy per-task answers or pages into a tool. A file under `tools/` that contains the start of an HTML document
  (`<!doctype html`, `<html`), the attribute `data-testid="scene"` outside a `[data-testid="scene"]` selector, or
  `__SHOWCASE__.getState` outside a quoted string is rejected. A tool may read the agent's `index.html`, serve it,
  render it and measure it; it may not contain one.
- Sibling imports are not available (`python -I`): one self-contained entry file.
- Use only what the task's environment facts list. Do not guess paths or packages; a tool that fails reports its
  stderr tail to the agent and to you.
- Keep tools general: the agent works on different pages; a tool is called on whatever `index.html` is in the
  workspace at the time.

## The reply

A reply is a sequence of file blocks and delete lines, then one summary line. Nothing else is read.

```
<<<FILE ADAPTER.md>>>
...the complete new content of the file...
<<<END FILE>>>
<<<FILE tools/wordcount/TOOL.json>>>
...
<<<END FILE>>>
<<<DELETE tools/old_tool>>>
SUMMARY: one line saying what changed and why
```

- `<<<FILE path>>>` ... `<<<END FILE>>>`: the whole file, replacing what was there. Paths are `ADAPTER.md` or
  `tools/<name>/<file>`; nothing else is writable. Each file within its size limit.
- `<<<DELETE path>>>`: removes a file; `<<<DELETE tools/<name>>>>` (or deleting a bundle's `TOOL.json`) removes the
  whole bundle.
- `SUMMARY:` ends the reply. A block with an invalid path or an oversize file is skipped and reported; a reply with no
  block and no summary is asked for once more.
- After the reply: the template check runs (a bad bundle is reported, then the Trainer's repair reverts its TOOL.json),
  the self-tests run when a sandbox runner is configured, and the catalog is rebuilt. A self-test runs the tool's own
  `test` in a scratch workspace of its own, then calls the tool with `example_args` in a fresh workspace holding the
  agent's best recent work (the version that was submitted), then, when that call returned images, once per probe
  page the task supplies (a page whose correct picture is known, such as a single colour). A tool counts as working
  when the call on the agent's work succeeds with a usable image and every probe shows its expected picture; a failed
  test or probe gets you one repair turn. You see all of it in the next step's material.
