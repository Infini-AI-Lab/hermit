# S1 review: requests to other streams

These follow from the review fixes D1 and D2 in `S1-env-review.md`. They supersede items 10 and 12 of
`S1-env-requests.md`, and correct the `code_roots` + `data` advice in its items 1 and 10. Items 1–9 and 11 still stand.

## S2 (pack tools)

1. **The actor now reads only the code the wrappers run.**
   - `build_kit` statically follows each wrapped script's imports of *sibling* modules (an `ast` scan). For
     `packs/officeqa/cli.py` that is `cli.py` + `otables.py`. It grants read access to exactly those files and a
     *listing* of `packs/officeqa/` (file names only).
   - `seed_notes.json`, `seed_rules.json`, `pack.py`, `rules.py`, `leak.py`, `observe.py`, `spec.py`, other packs and
     `harness_core/` outside `tools/` are unreadable in every arm. This was verified in the sandbox with the real
     `find_tables`/`show_table`.
   - **Constraint:** an actor-side tool must import its helpers with plain `import x` / `from . import x` / `from x
     import y` statements (anywhere in the file). `importlib.import_module`, `__import__` or `open(__file__-relative
     data)` will fail with `Operation not permitted` inside the sandbox. A sibling *package* (a directory) is refused
     at `build_kit` time.
2. **Optional, recommended:** move the actor-side CLI (`cli.py`, `otables.py`) into its own directory, for example
   `packs/officeqa/tools/`. Then the directory listing the actor gets no longer shows the seed and rule file names.
3. Any edit to a granted code file between preparation and the end of an episode now fails `verify_kit`
   (`build_settings` refuses to start, and `result.json kit_after.intact` becomes false). Do not edit pack tools while
   a campaign runs.

## S4 (calc / submit)

4. `harness_core/tools/calc.py` and `submit.py` are granted file by file. Their `harness_core.contracts` import
   already falls back to literals under `-I`, and it must stay that way: `harness_core/contracts.py` is not readable
   by the actor.

## S6 (orchestration, audit)

5. **Drop `data=(code root,)` for tool directories.** `run_actor` grants code from the marker (`marker['kit']['code']`)
   itself. Pass `data` only for real data, such as the tables index directory.
6. **Embedded submit URL.** If you pass `submit_url` to `prepare_actor_workspace`, it must equal the broker route of
   the endpoint later given to `run_actor`. `build_settings` now refuses a mismatch. Simplest: do not pass it, and let
   `acting` derive `HARNESS_SUBMIT_URL` from the endpoint.
7. **Workspace paths must not contain `:`,** because it would split the kit PATH entry. `build_kit` refuses them.
8. **The marker is now unreadable to the actor** (`deny file-read-data`; `ls` still shows it). Put nothing
   model-facing in `marker_extra`, and record note ids there freely: the actor cannot read them.
9. **Audit:** `invocation.json` gains `code_read {files, dirs}`. Gate on `kit_after.intact`, which now also covers
   the code files.
10. **Decision (user):** `features.multi_agent` / `agents.enabled` are on for every arm (the v8 FULL values). Sub-agent
    requests reach the same broker port. S3's ledger and controller must either attribute them or the flag should be
    turned off for v9. This is not changed here, because DESIGN says "Codex config: as v8".
