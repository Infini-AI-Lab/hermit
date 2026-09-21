# hier_search

A starting adapter for search agents: a small local model reads full documents
on the acting model's behalf and reports back. The acting model keeps its own
plain `search` tool and may use `deep_search(goal, query)` or
`read_for(goal, docids)` from here when it wants documents read outside its
context.

The initial version is deliberately plain: one retrieval, one reader call with
an extract-or-NO_RELEVANT_INFO prompt, the report returned unchanged. It needs
three bindings from the framework: `search`, `documents`, and `small_model`
(see `harnessadapter/adapter/bindings.py`). The `hier_search` Evolver changes
what the reader is asked, how documents are read, and what the acting model is
told, with the objective of keeping answers correct while lowering the acting
model's cost.
