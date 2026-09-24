# Two initial architectures

An architecture is a starting adapter repo with no learned task knowledge. Evolution copies it into a candidate. The source stays unchanged. An evolved adapter uses the same manifest contract and can be updated again.

| | Prompt-only | MWM |
| --- | --- | --- |
| Built-in name | prompt-only | mwm |
| Core directory | harnessadapter/architectures/prompt_only | harnessadapter/architectures/mwm |
| Acting context | ADAPTER.md | Generated from the MWM package |
| Adapter tools | None | One wm tool |
| Adapter MCP server | Not registered | Started by the acting host |
| Initial native roles | None | None; roles can be defined later |
| Representation | One learned-context document | Memories, examples, tools, roles, briefing, journal, and cases |
| Default update method | Generic prompt-driven Evolver | Bundled MWM Evolver |

## Repository layout

```text
harnessadapter/architectures/
  prompt_only/    Initial prompt structure
  mwm/            Initial world-model structure
examples/
  minimal_shop/   Populated integration demo
```

Architecture sources are reusable starting points. Evolved Adapters are separate outputs; the current CLI stores candidates under .demo/evolution/. The built-in names prompt-only, mwm, and minimal-shop are unchanged.

## Prompt-only

```text
adapter.json      Context path; empty tools list
ADAPTER.md        Initially no learned instructions
EVOLVER.md        Guidance for maintaining the prompt
README.md         Human-facing usage
```

This makes prompt-oriented experiments possible without a tool service. It is not an implementation of GEPA or SkillOpt. Their optimization algorithms and evaluation requirements would need their own integrations.

The current manifest retains the name harnessadapter.python.v1 for compatibility; a static Markdown context and empty tools list require no Python code in the adapter. The framework itself still uses Python and its shared dependency environment.

## MWM

```text
adapter.json · ADAPTER.md · EVOLVER.md
interface.py · mwm_core/
memory/           Scoped environment knowledge
examples/         Worked cases
tools/            Learned program bundles and tests
agents/           Optional SPEC.md native-role definitions
consumption/      Acting briefing
meta/             Journal and persistent cases
```

MWM retains its reference layout and update conventions. The framework owns MCP wrapping and host conversion. Other architectures need not adopt MWM directories or claims.

## What may evolve

All core files are editable, subject to the interface contract. For a prompt-only study, the update instructions restrict the chosen search space to context; that is a study choice, not a platform-wide limitation. Adding tools later will cause the connector to register MCP on the next deployment.

minimal-shop remains a small, pre-populated integration demo. It is not a third empty architecture. A 4B search-helper architecture remains a future example.


## Animation Workshop (experimental)

`animation-workshop` organizes methods, readable and modifiable implementations, and integration examples for web physics animation. It starts empty and uses its own Evolver, not the MWM lifecycle. See [Animation Workshop](animation-workshop.html) for source paging, trial previews, and evaluation limits.


## Lemma board (multi-agent, experimental)

`lemma-board` is a collaboration protocol rather than a knowledge store: N
acting agents prove the same Lean theorem in parallel and share work only
through a board of lemmas, reached by six `board_*` MCP tools (list, post,
claim, done, blocked, pull). Communication is asynchronous; no agent waits
for another. Proof verification is the launcher's job, outside the adapter.
The evolvable parts are the acting context and the tool semantics. See
`harnessadapter/architectures/lemma_board/README.md`.
