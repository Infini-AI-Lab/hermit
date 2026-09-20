# Animation Coach runtime bridge

This variant reuses MWM's adapter structure, journal rolling, docket caps, and executable admission. Its learning policy is entirely defined by method/CONTRACT.md. The original MWM contract, agenda, and learning skills are not loaded for this variant.

Use the evolution MCP file capabilities with area `adapter` for the writable candidate, `evidence` for read-only Experience, `method` for these instructions, `environment` for configured access, and `history` for the previous adapter. Temporary work belongs in `scratch`. Read evidence/README.md for record addresses; do not assume a particular JSON schema. Use read_image to inspect supplied images; mentioning their paths does not inspect them.

Only candidate adapter and scratch are writable. Read environment/README.md or get_policy before exploration. Network and external reads follow that policy. Optional validation is visible learner data, not hidden evaluation. No browser, grader, or renderer is implicitly available.

MWM learned Python tools retain tools/<name>/{TOOL.md,run.py,test.py}, a literal SCHEMA dict, and run(**kwargs). Hosts call them through wm(name, args). Use test_python for admission; ordinary command checks do not replace it. If the Linux bubblewrap backend is unavailable, report the limitation and retain prose or an explicitly unadmitted candidate. Never bypass admission in interface.py. Native role definitions remain agents/<name>/SPEC.md and are launched by the acting host.

The framework rolls older journal sessions and caps docket cases before learning. Finalization checks learned tools. The framework preserves run snapshots and reports; no new candidate is automatically activated or declared better. Existing MWM host consumption is unchanged unless the learner explicitly edits the candidate; inspect and report any such edit.
