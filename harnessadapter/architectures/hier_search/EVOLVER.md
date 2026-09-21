# Evolving this adapter

This adapter gives an acting model a small local reader. The acting model
answers multi-constraint questions by searching a fixed corpus; it always has
the plain `search` tool (top-5 snippets) and may additionally call the tools
declared here, which have the local model read full documents and report.

Objective, in this order: the acting model must answer at least as many
questions correctly as before, and it should spend less (fewer main-model
tokens, fewer search rounds) to get there. The local model's tokens are
recorded but are not the cost being minimized.

Everything in this directory may change: ADAPTER.md (what the acting model is
told), the tool declarations and their descriptions in adapter.json, the
implementation in interface.py (how many documents, how they are cut, how many
reader calls, caching, what is returned), the reader prompts under subagent/,
and config.json. The loadable contract is the only fixed part: every declared
tool must import and run, with a JSON-schema input and a JSON object output.

Resources bound by the framework, available to interface.py through
`_resources`: `search` (query -> ranked docids with scores), `documents`
(docid -> full text), `small_model` (chat completions on the local reader).
Do not assume network access or any other service.

Two boundaries do not evolve:
- The reader reports what documents contain. It does not decide whether a
  candidate satisfies the question and does not tell the acting model what to
  search next.
- No fact from the learning material enters this directory: no answers, no
  entity names, no document contents, no question-specific examples. The
  material shows how the adapter was used; it is not knowledge to store.

The learning material README describes the records: per-question trajectories
with every tool call (which tool, goal, query, docids, reader input and
output, tokens), the acting model's messages, the judge verdict with the
reference answer, and per-batch totals. Read the trajectories before the
totals; the totals say what changed, the trajectories say why.

Layout:

    ADAPTER.md            what the acting model is told (the Context export)
    adapter.json          tool declarations: names, descriptions, schemas, entrypoints
    interface.py          tool implementations; receives the bindings as `_resources`
    subagent/report.md    the reader prompt; {goal}, {query}, {documents} are filled in
    config.json           k, doc_char_cap, reader max_tokens, and other knobs
    README.md             a short description for people

To try a change, the evolution session offers `try_tool(name, arguments)`,
which loads this candidate with the real bindings and calls one declared tool,
and `small_model(messages, max_tokens)`, which calls the reader directly.
