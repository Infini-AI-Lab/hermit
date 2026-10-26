# ReHarness adapter: structure

The adapter is the textual harness state of ReHarness: a bank of reflections, one per past instance of the stream.

- `ADAPTER.md` (learnable, UTF-8 text, at most 40,000 bytes): the decision agent's instructions. The retrieved
  reflections are appended below it for every instance, in the `[RANKED REFLECTIONS]` layout. The ReHarness
  Evolver does not change this file; a seed adapter may carry the task's instructions here.
- `index.json` (learnable): `k` (reflections retrieved per instance), `retrieval` (`embedding` or `bm25`),
  `embedding_model`, `dim` and `memory` (`reflection`, or `trajectory` when the Evolver stores raw trajectories, its
  Top-K Raw baseline; the reader renders the block's labels from it). The Evolver's bootstrap writes it from its
  parameters; every embedding in the bank must come from the model it names.
- `reflections/<id>.json` (learnable, one file per reflection, at most 400,000 bytes each): `id` (the memory id,
  the file's name), `task`, `step`, `created`, `query` (the instance's input, what retrieval compares),
  `reflection` (the text the decision agent reads), `embedding` (the query's unit vector as base64 half floats,
  with its model) and `revisions` (one record per backward revision: the step, the instance that triggered it, the
  rank at which the reflection was retrieved, the size of the change).
- Fixed: `interface.py` (the reader), `reharness_core/` (bank, retrieval, rendering), `check.py`, this guide and
  `README.md`.

A valid change keeps every reflection file parseable with those fields and consistent with `index.json`
(`check.py` verifies it). The reader needs no other file: it ranks the bank's `query` texts against the current
instance's input and renders the top k.
