# Animation Workshop

This adapter supplies reusable making capabilities for web-based physics animations: concrete methods, readable/adaptable implementations and integration examples. It is a construction assistant, not a guaranteed physics engine or a mandatory scene template. The current user task defines the scene; historical examples and preferences retain their stated scopes.

Use the capability map and catalog to select relevant material by behavior and assumptions. Retrieve methods and source with `workshop(name="read", args={"path":"implementations/example/file.mjs"})`. Reads return text, total_chars, next_offset, eof and sha256. Follow continuation offsets when needed and pass expected_sha256 to detect revision changes. `list` exposes entries and `search` finds matching lines. An incomplete page is not complete source.

Before adapting, identify what the implementation owns, what scene inputs or edits it expects, and which useful properties should remain true. Copy or modify source in your task workspace. Examples demonstrate integration; do not inherit their geometry, camera or old task constraints without checking applicability. If you replace the implementation, verify that the behavior you intended to reuse survives, or document why another approach fits better. Reusing function names or reading an example does not demonstrate reuse of its mechanism. Check the integrated result, not only an isolated helper.

Do not edit the shared adapter from the acting run. Your adaptations may become future LearningMaterial. Integrate runtime logic and assets into the final animation so playback needs no MCP. No role, physics solver or learned helper is preseeded in the empty architecture.
