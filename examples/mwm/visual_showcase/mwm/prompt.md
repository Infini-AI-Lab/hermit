Maintain an environment-centered world model using the MWM method. Begin with the adapter's EVOLVER.md and the evidence store's README.md. Follow the original contract, agenda, skills, and patterns supplied in the method area. The deployment bridge explains how to access them through framework capabilities.

Repair contradicted or outdated knowledge, retain valid historical readings and their scope, maintain the persistent docket, and update the host briefing. Add executable capabilities or native roles only when the method's evidence and admission criteria justify them. Return a reviewed candidate rather than modifying the deployed source.


Read adapter/EVOLVER.md for the core structure and evolution guidance. Read evidence/README.md for the evidence storage and record format. These documents describe the task; they do not grant permissions.

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


REVIEW SESSION #{session}.
Learn a compact, evidence-grounded adapter for this familiar offline visual
development task family ({case}) from the supplied GLM episodes ({runs}), starting from {start}.
Follow the original MWM method and contract. Inspect the task, actual source, browser
observations and at least one real original PNG for EACH supplied run of {case}. You are
the learner; no external subjective critique, score or other learned visual adapter is provided.
Separate observed behavior from your interpretation and proposed future remedies. Preserve
good approaches as well as observed limitations; do not invent failures, scores or winners.
Prefer reusable checks and concise visual principles over copying solutions. Do not copy
complete HTML, shaders or whole implementations. No learned executable changes are allowed.
The transport exposes ONLY list_files, read_file, read_image, write_file, delete_path,
move_path, validate, get_policy. Method mentions of other tools do not make them available.
Read areas remain native adapter/evidence/history/method/environment/scratch. Candidate
writes are limited to Markdown below memory/ and examples/, consumption/brief.md,
meta/journal.md, meta/journal-archive.md and meta/cases/. Scratch is isolated text only.
No changes to core, manifest, interface, tool/agent definitions, Python, JS or HTML files.
Keep consumption/brief.md <={max_brief} characters, linking concise durable knowledge and examples.
Verify any interface/tool claims against the provided source. Do not execute candidate code.
Validate before your final report.
Budget: {max_seconds} seconds, {max_requests} API requests, {max_tools} requested tools, {max_completion} completion tokens total,
{max_response} maximum per response{reasoning_note}. Only the latest read_image batch is kept as images (8 million
base64 characters max); earlier textual observations remain. Read images early enough for
a subsequent model request to receive them. Finish with a factual changes/evidence/limits
report; this produces a review candidate, never automatic performance promotion.

Read method/CONTRACT.md fully, then environment/README.md and evidence/README.md. Read meta/journal.md and the relevant docket cases. Use the pinned skills and patterns when the agenda calls for them.

[Memory index]
{memory_index}

{docket}

consumption/brief.md: {brief_chars}/30000 chars

{notice}

Raw evidence keeps its supplied format. The framework computes no outcome statistics from it. Follow its README for addresses.

Framework execution policy:
# Execution environment and evidence

The adapter candidate is readable/writable. The evidence store is always readable and read-only; its README defines its format. Method materials and history are also read-only. Only adapter and isolated scratch accept writes.

Additional environment read scope: {"paths": []}
Default command working directory: {working_directory}
Agent tool/command network: disabled. Model API transport remains available.

Use get_policy for absolute roots; read_path/list_path for authorized environment files; run_command for sandboxed exploration; fetch_url for HTTP(S) when enabled. No benchmark-specific environment or evaluator is implicitly provided. Environment directories stay read-only even when used as a command working directory. MWM executable admission still uses its stricter test_python sandbox; ordinary run_command success is not admission.

Read and obey it before interacting with the environment. All edits and execution use the evolution MCP tools. Native host tools are not an alternate permission path.