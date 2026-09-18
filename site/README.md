# Harness Adapter design website

An interactive English design notebook presenting the proposed architecture. Static HTML, CSS, and JavaScript; no dependencies, external fonts, image requests, or model calls.

## Run locally

From the repository root:

```sh
python3 -m http.server 8873 --bind 127.0.0.1 --directory site/dist
```

Visit <http://127.0.0.1:8873>. You can also open `dist/index.html` directly; clipboard buttons in local-file mode depend on browser permissions.

## Contents

- An architecture diagram linking Acting Harness, Harness Adapter, and Meta Harness, with an optional Bootstrap / Connector and CLI / Reef orchestration modes.
- Repo explorers for a skill, the MWM reference implementation, and a 4B tool, showing Markdown, MCP, and optional role exports.
- Minimal role definitions: name, description, and instructions, with other settings inherited from effective host defaults.
- Native/external and optional-helper integration paths; evolution produces candidates without replacing roles in the current run. New definitions load in the next run.
- Interface sketches for both entry points, scope, and open questions.
- The v0 contract: Context / MCP / optional Agent Definitions, ready before formal tasks; integration helpers remain optional.
- A discussion archive at `#archive`, with the full note at `dist/discussions/2026-09-18.html`.
- `dist/design.md`: a downloadable design snapshot.
- `dist/adapter-contract-v0.md`, `dist/subagent-fields-2026-09-18.md`, and `dist/design-discussion-2026-09-18.md`: published copies of the corresponding source documents in `docs/`.
- `dist/architecture.drawio`: editable diagram source.

Interfaces, layouts, and compatibility are design proposals. The simulation does not modify real data. The site has not been deployed publicly.

After editing the three source documents listed above, run `python3 site/sync-docs.py` to refresh their static copies and rebuild the discussion article.

The current ownership section shows core-only adapters and the framework-owned wrapper. The MWM explorer displays source files from the reference core. The lifecycle section links the real Evolver agenda and a dated integration report, with synthetic-fixture and execution-boundary limitations.

The execution-policy panel previews the implemented TOML configuration for Codex/Claude Code, additional environment reads, and networking. The fixed adapter/evidence write boundaries never change in that preview. It does not execute or alter real permissions.

## Documentation site

The guide lives at `/docs/`, generated from `docs/guide/`. Run `python3 site/build-docs.py` after changing guide sources. Run `python3 site/sync-docs.py` to refresh the live architecture explorers and design archives. Prompt-only and MWM are implemented architectures; the 4B example is illustrative.
