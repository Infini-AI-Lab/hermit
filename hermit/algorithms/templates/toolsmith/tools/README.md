# Tool bundles

One directory per tool, `tools/<name>/`, holding `TOOL.json` and the program it names. The agent's harness lists each
admitted bundle as a callable tool (its `name`, `description` and `parameters`) and runs it on request; the program's
text, and the images it writes, come back to the agent. Directories without `TOOL.json`, and names starting with `_`
or `.`, are not tools.

`TOOL.json` fields:

- `name`: equals the directory name; `^[a-z][a-z0-9_]{0,63}$`; not a primitive's name (write_file, read_file,
  replace_text, run_command, inspect_page, wm, tool_catalog, tool_call, context_package).
- `description`: non-empty, at most 1024 characters: what the tool does, what it returns, when to call it.
- `parameters`: a JSON Schema with `"type": "object"`; keywords type, properties, required, additionalProperties,
  description, enum, default, items, minimum, maximum; `"required": []` — every parameter optional, the tool runs
  with `{}`.
- `entry`: the program file in this directory: `run.py`, `run.cjs`, `run.js`, `run.mjs` or `run.sh`.
- `test`: optional self-test, `test.py`, `test.cjs` or `test.sh`; no arguments; exit 0 = pass.
- `example_args`: optional, default `{}`: arguments fitting `parameters`, used by the contract self-test.
- `timeout_s`: optional, default 60: an integer from 1 to 90.

Other files: only `.py .md .json .txt .js .cjs .mjs .sh`, UTF-8 text, at most 60,000 bytes each, no name starting with
a dot, at most 40 files under `tools/`. At most 8 tools are admitted, alphabetically. No file may contain the start of
an HTML document, the brief's scene attribute outside a selector, or the page's state hook outside a quoted string: a
tool reads, renders and measures the agent's page; it never contains one.

How a program runs: the interpreter follows the extension (`.py` → the venv `python3 -I -B`, `.cjs .js .mjs` → `node`,
`.sh` → `bash`); the bundle is mounted read-only at `/tool` and the program starts as `/tool/<entry>`; the working
directory is the agent's workspace (read-write; it holds `index.html`); the arguments arrive on stdin as one JSON
object (`{}` when none); the result is the last stdout line that is a JSON object, `{"text": "...", "images": [...]}`
— `text` required, `images` optional PNG paths inside the working directory (the first 5 are shown to the agent);
exit code 0 means success. No network; `/tmp` is discarded after the call; Python sibling imports are unavailable.

## Example: `wordcount`

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
