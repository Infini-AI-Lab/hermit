# Physics Builder runtime bridge

This preset reuses MWM storage, journal rolling, docket limits and executable admission. It loads its own learning contract, not the original environment-only MWM contract or the Animation Coach contract. No new runner, tool backend, simulator or model is installed by selecting this preset.

The adapter guide describes the current layout and public exports. This method replaces its environment-only or advice-only learning objective where they conflict. Existing evidence and uncertainty remain valid; framework permissions and executable admission cannot be overridden by this guide or by edits to the candidate.

## Available areas and execution

Use evolution MCP file tools with `area="adapter"` for the writable candidate, `area="learning_material"` for read-only learning inputs, and `method`, `history`, or `environment` for their read-only materials. The legacy `evidence` area and physical `evidence/` snapshot refer to the same LearningMaterial. Use scratch for temporary experiments. Read LearningMaterial/README.md through the tool area, not an assumed physical directory name. Read get_policy and environment/README.md to learn the actual command paths and network grants.

read_image returns supplied image pixels. run_command can run authorized diagnostic programs in scratch or the candidate, within the current execution policy. fetch_url is usable only if networking is enabled. A browser, renderer, physics engine, grading service and external environment actions are not implicitly available. Do not reinterpret imported logs or self-reports as experiments you performed.

## Deployable computational capabilities

The current MWM callable format is tools/<name>/{TOOL.md,run.py,test.py}. run.py declares a literal SCHEMA and run(**kwargs); returned values must be JSON-serializable. Hosts call admitted tools using wm(name=<tool name>, args=...). TOOL.md documents scope, provenance, invocation and verification. Keep dependencies within the declared supported runtime.

test_python is the admission path for these learned Python tools. It currently requires the Linux bubblewrap backend, which is unavailable on this Mac. run_command success is useful diagnostic evidence but does not admit a tool. Do not bypass this by importing candidate code in an unprotected process, modifying the dispatcher to execute unadmitted programs, or labeling an unavailable check as passed. Explicitly record this capability blocker.

Reusable implementation source can instead be stored as an artifact readable through wm read, for example under resources/. It is code for the acting agent to inspect, integrate and test during construction, not an admitted MCP executable. Give its language, entrypoints, input/output conventions, dependencies, scope and test evidence. Reading source does not execute or validate it. The framework does not automatically compile, install or insert it into the final HTML.

Journal/docket maintenance and final learned-tool checks run as in MWM. Candidate readiness means structural checks passed, not that any animation improved. Actual acting use is observed only in later run records; never infer a tool call from a briefing mention. There is no automatic publication or activation.
