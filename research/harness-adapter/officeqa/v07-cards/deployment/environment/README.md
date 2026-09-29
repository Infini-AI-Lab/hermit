# OfficeQA environment (prompt-only adapter)

The same 697 parsed TXT documents and FTS5 helper/index used by actors are granted
read-only; exact paths are in the session goal and get_policy. The helper exposes
OfficeQACorpus.search, read and list_files. Direct file inspection and Python
calculations are available through run_command/bash. Commands have no network.

The adapter is one document, ADAPTER.md, which the next actors receive verbatim
inside their AGENTS.md. There is no MCP server, no wm tool, no roles, no
executable tools and no container in this profile. Only the current completed
batch receives post-submission reference answers, strict/1% grades and
per-episode failure records (failure.json). verify_answer grades a candidate for
a released ordinal only; verify_cell reads one corpus line; both are the checks
that lessons cite and the controller replays. The postmortem directory holds this
block's verdicts and your scratch/postmortem/<nnn>.json records. An optional
reference shelf of extracted reference text is readable here and never by
actors. No external resource, other model, future answer or master key is bound.
