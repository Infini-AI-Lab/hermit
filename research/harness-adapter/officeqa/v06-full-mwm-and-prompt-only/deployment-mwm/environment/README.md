# OfficeQA environment

The same 697 parsed TXT documents and FTS5 helper/index used by actors are granted
read-only; exact paths are in the session goal and get_policy. The helper exposes
OfficeQACorpus.search, read and list_files. Direct file inspection and Python
calculations are available through run_command/bash. Commands have no network.

Only the current completed batch receives post-submission reference answers,
strict/1% grades and per-episode failure records (failure.json). The controller
grades verdicts through verify_answer (released ordinals only) and reads corpus
lines through verify_cell; both are the checks that lessons cite. The postmortem
directory holds this block's verdicts and your scratch/postmortem/<nnn>.json
records. An optional reference shelf of extracted reference text is readable
here and never by actors. The Linux executable-tool sandbox mounts only the
adapter, read-only. No external resource, other model, future answer or master
key is bound.
