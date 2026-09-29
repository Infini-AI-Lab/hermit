# S2 (OfficeQA pack) — requests to other streams

None of these block S2. Each one names the owner, the reason, and what S2 does in the meantime.

1. **S4 ledger: the per-output observation cap drops pack cells.** (`harness_core/ledger.py`, `Ledger(max_obs_per_output=400)`)
   - One `show_table` page can show more than 400 cells. The stitched FFO-3 page (`treasury_bulletin_2004_03.txt:728 --stitch`) shows 37 columns × 12 rows.
   - Cells are emitted row by row, so the cap silently drops the last rows of a page.
   - Request: raise the cap to about 2,000 for outputs whose observations come from `pack.observations`, or let the pack declare its own cap.
   - The pack already caps its own output: 4,000 cells, and 400 raw or corpus lines per command.
   - Meanwhile: nothing breaks, but the ledger does not record those cells.

2. **Pytest marker registration (S6 or whoever owns the test configuration).**
   - The tests use `@pytest.mark.slow`. Without a registration, pytest prints `PytestUnknownMarkWarning`.
   - Request: add `markers = slow: needs the full tables index (~30 s)` to a `pytest.ini`, `pyproject.toml` or `tests/conftest.py`.
   - Optionally also add `addopts = -m "not slow"`.
   - S2 may not create either file, so the warning stays until then. It is harmless.

3. **Contracts, and S4 `count_mismatch`: answers that mix numbers and words.**
   - Q103 asks for `[<gini>, 'surplus' or 'deficit']`.
   - `QuestionSpec` has no field for the allowed words. `count_mismatch` counts only `pack.numbers(answer)`, so it fires on a correct mixed answer: it expects 2 values and finds 1 number.
   - Request: either add `QuestionSpec.allowed_words` (the replay parser's quoted words, `checkers.py:239`), or let `count_mismatch` count words that the question quotes.
   - S2 fills the field as soon as it exists.

4. **S1 kit: the L1 tool wrappers.**
   - Install `bin/find_tables`, `bin/show_table` and `bin/printings`. Each runs `<kit python> -I <plumbing>/packs/officeqa/cli.py <tool> "$@"`.
   - The kit python is `frameworks-v9/actor-python/bin/python3`. This path is tested: a clean environment, `-I`, 0.4 s.
   - Environment variables:
     - `OFFICEQA_CORPUS=<prepared/agent/corpus>`
     - `TABLES_INDEX=<the run's tables.sqlite>`
   - Read roots must include:
     - the index file;
     - `packs/officeqa/cli.py` and `packs/officeqa/otables.py` (a copy of just these two stdlib files is enough).
   - README text: `OfficeQAPack().usage()` (tool usage) and `OfficeQAPack().task_conventions()` (the two published paper rules, for all arms).
   - `pack.tools` maps each command name to `cli:<function>` inside `packs.officeqa`.

5. **S6 prepare: the tables index per run root.**
   - Option 1: build it with `python -m packs.officeqa.cli build --corpus <prepared/agent/corpus> --out <prepared/agent/tables.sqlite>`. Measured: 20.7 s, 146.8 MB, written read-only (0444).
   - Option 2: copy `frameworks-v9/officeqa-tables-index/tables.sqlite`, then check that its `meta.corpus_manifest_sha256` equals the manifest hash of the prepared corpus. That value is `8b4d7329e3824d9f1ff2bc00ac5c677c89fca5904261624fede4f3fb4dc150b0`, from `opus-officeqa-133/corpus`, which is byte-identical to the v8 agent corpus.
   - The broker and gate process needs the same two paths: construct `OfficeQAPack(corpus, index)`, or export `OFFICEQA_CORPUS` and `TABLES_INDEX`.

6. **S5/S6 seed parity: how the text arms see the seed notes.**
   - `OfficeQAPack().seed_text()` (1.4 KB) renders all 5 pack seed notes. `OfficeQAPack().seed_notes()` returns them as `NoteSpec`.
   - 4 of the 5 notes have the trigger `'*'`, but `NOTES_PER_EPISODE = 3`. Rendered as scoped notes, one would never show.
   - Recommendation, per the DESIGN wording "written once as W0 text": render `seed_text()` in full into the text arms' W0 text. Keep the scoped-note budget for learned notes.
   - Contracts could add `seed_notes()` to `Pack`. `OfficeQAPack` already has it.

7. **S4, for information only: offer wording and the check limits.**
   - The pack templates never reference `{offer.*}`. A pack finding therefore still shows when its offer cannot be computed (for example, when the calc that produced the answer did not read the cell directly), and the gate adds the Offer and Accept lines when an offer exists.
   - The pack's offers delegate to `harness_core.offers.recompute`. The tests assert only that the substituted locator appears in the derivation, because its wording changed during this wave.
   - The pack rules query the tables index and the corpus. Measured worst case over the v8 replay: 0.82 s per gate check. They do not need the sandbox runner unless an offer re-runs a calc.
