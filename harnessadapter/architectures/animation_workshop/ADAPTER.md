# Animation Workshop

This adapter supplies accumulated making experience for web-based physics animations: concrete methods, readable/adaptable implementations, and examples showing how they fit together. It is a construction assistant, not an automatic guarantee of physical realism or a mandatory engine.

Use the current task as the authority. Inspect the catalog for a relevant method or implementation before reinventing its core work. Retrieve what you need using `workshop(name="read", args={"path":"implementations/example/file.mjs"})`. Each result has `text`, `total_chars`, `next_offset`, `eof` and `sha256`. Continue with the returned offset when useful; concatenate only text fields. Supply `expected_sha256` to detect changes between pages. Do not treat an incomplete page as complete source. `list` gives the catalog; `search` finds matching lines.

Read source critically. Copy and adapt it in your task workspace, preserving its applicable assumptions and useful properties. Methods and complete small examples are references, not immutable rules. Check the actual integration; a helper test does not prove the rendered animation works. A prior working implementation is useful evidence, not a universal recipe.

Do not edit this shared adapter from the acting run. Your adaptations and observations may be incorporated by a later Evolver. The final animation must be standalone: integrate necessary runtime code and assets under the task's constraints; do not call this MCP service during playback. No role, physics solver or executable helper is preseeded.
