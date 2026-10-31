The executable layer: programs the hosts can CALL through wm(<name>,
args), one directory per tool. Per directory (unified v1.1 format, every venue):
- TOOL.md — first line: the catalog one-liner a host reads; then when/why.
- run.py  — a literal SCHEMA dict (name, description, parameters — the
  host fills them in) plus run(**kwargs) returning JSON-serializable
  output; stdlib-only. Executed sandboxed: no network, no environment access, the
  repository read-only at /w, only the arguments as input.
- test.py — a plain script that raises on failure. Admission: a tool
  whose test does not pass in the sandbox is parked in tools/_rejected/,
  not served. The SCHEMA description is what a host reads when deciding
  whether to call it. TOOL.md doubles as the prose fallback: it states
  what the tool does plainly enough to follow by hand if the program is
  down.
