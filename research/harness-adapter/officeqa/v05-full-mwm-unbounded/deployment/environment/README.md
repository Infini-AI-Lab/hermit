# OfficeQA environment

The same 697 parsed TXT documents and FTS5 helper/index used by actors are granted
read-only; exact paths are in the session goal and get_policy. The helper exposes
OfficeQACorpus.search, read and list_files. Direct file inspection and Python
calculations are available through run_command/bash. Commands have no network.

Only the current completed batch receives post-submission reference answers.
The Linux executable-tool sandbox mounts only the adapter, read-only. The
learner's command sandbox separately provides corpus access for investigation.
No external resource, other model, future answer or grader service is bound.
