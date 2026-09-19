# MWM deployment bridge

The learning method is the original MWM V2 contract, agenda, pinned meta_skills_v110, and pattern library, provided in the read-only method area. Read method/CONTRACT.md first, and follow its agenda. This file describes only the transport and ownership adaptation.

## File interface and virtual mounts

Use read_file/list_files with these intrinsic areas; evidence remains readable regardless of additional environment grants:

- `/world-model/...` or repo-relative artifacts: area `adapter`, path relative to the core.
- `/evidence/...`: area `evidence`, original records; read-only.
- `/skills/...`: area `method`, path `skills/...`; read-only.
- `/patterns/...`: area `method`, path `patterns/...`; read-only.
- `/environment/...`: area `environment`; read-only capability description and any explicitly supplied public material.
- `CONTRACT.md`: area `method`, path `CONTRACT.md`.

Write, move, and delete operations are granted only for adapter and scratch. Every core file may evolve, including the consumption implementation and export declaration. Framework files, raw evidence, method instructions, and controller state are outside the editable core. Keep the public adapter contract loadable; MWM's internal conventions are the initial method design, not the universal adapter format.

## Executable verification

The original captest invocation maps to `test_python(directory="tools/<name>")`. This runs test.py only when the required Linux bubblewrap backend is available. An unavailable backend is not a passing test. Do not claim executable verification in that case; retain a prose representation or an explicitly unadmitted candidate bundle. Never replace sandboxing with an in-process import or a top-level MCP function.

Learned tools retain tools/<name>/{TOOL.md,run.py,test.py}, literal SCHEMA, and run(**kwargs). Hosts call them through wm(name=<SCHEMA name>, args=...), not separate MCP tool names. Roles retain agents/<name>/SPEC.md, including provenance and scope; no useful role is pre-seeded. The framework exports these into native host role formats at deployment.

## Lifecycle

The method prepares journal rolling, docket caps, and status before each session. A read-only history area, when available, preserves the prior core snapshot and pre-maintenance content. The framework retains immutable session snapshots and hashes instead of editing .git inside the core. There is no automatic activation.

The framework-generated environment README and get_policy define this run's additional readable directories and network access. Use read_path/list_path to inspect permitted environment files, and run_command/fetch_url for permitted interaction. Network is enabled by default and may be disabled by the user. No benchmark-specific environment actions are implicitly supplied. Do not claim an observation you did not make. Context/run/delegation budgets are owned by the acting host in this standalone integration, not a benchmark evaluator.
