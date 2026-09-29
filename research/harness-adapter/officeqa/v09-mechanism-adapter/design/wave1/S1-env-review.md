# S1-env adversarial review (wave 1)

- **Scope:** `kit.py`, `acting.py`, `paths.py`, `tests/test_v9_kit.py`, `tests/probe_kit.sh`, `frameworks-v9/`, and
  the report `S1-env.md`.
- **Rules followed:** no paid call, no network except 127.0.0.1, no reference answers or graders opened, no
  credential file read.
- **Files edited:** only files S1 owns (`kit.py`, `acting.py`, `tests/test_v9_kit.py`), plus this report and
  `S1-env-review-requests.md`. `paths.py`, `probe_kit.sh`, `contracts.py` and `frameworks-v9/` are unchanged. The
  pre-review copies are in the session scratchpad (`orig/`).

**Terms.**
- **Seatbelt** is the macOS sandbox. `sandbox-exec -p <profile>` runs a command under a rule file (the profile).
- A **`subpath`** grant covers a whole directory tree. A **`literal`** grant covers exactly one path: a file, or a
  directory's listing only.
- The **marker** is `.acting-prepared.json` in the workspace. The controller writes it and it records the arm and the
  kit hashes.
- An **import closure** is a script plus every module it imports, followed transitively.

## 1. Verdict

The implementer's core claims hold. I reproduced them:
- the kit and PATH repair, python/numpy/pandas/FTS5, rg, heredocs;
- the denied reads, writes and egress;
- the `.harness` write protection;
- real Codex starting in the narrowed profile without the venv, harnessadapter or cua_node;
- the effect of the permissions key.

The frozen copy is faithful. `tree_manifest(copy) == manifest.json`. Against the source, the only difference is one
`.pyc` that the writable source runtime created after the copy. The rg sha equals the app's (`05fe4a08…`).

I found **two validity defects in what the actor can read**. Both are now fixed:
- **D1:** every arm could read all of `packs/` and `harness_core/`, including the seed notes and seed rules.
- **D2:** the marker exposed the arm label to the model.

I also fixed four smaller robustness defects (D3–D6). Section 4 lists what remains open.

## 2. Tests: before and after

Command, run from `officeqa-pro133-v9-plumbing/`:
`/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/adapter-comparison-work/.venv/bin/python -B -m pytest -q -p no:cacheprovider tests/test_v9_kit.py`

| | Result |
|---|---|
| Before the review (implementer's code) | `27 passed in 8.37s` |
| After the fixes | `30 passed in 9.86s` (slowest: `test_real_codex_exec_in_v9_sandbox` 2.43 s) |
| Other streams' tests (`tests/ --ignore=tests/test_v9_kit.py`), for information | `201 passed, 15 skipped in 134.85s` |

- The file has no slow or live markers, and no test comes near 60 s, so nothing needs marking.
- The "live" tier is `test_real_codex_*`. These run the real pinned Codex against a fake upstream on 127.0.0.1, at $0.

**Tests added or extended in this review:**
- `test_build_kit_code_closure` (new). Covers the sibling-import closure, the directory listing only when needed,
  refusal of a sibling package, and the real `cli.py` closure (no `.json`, no `harness_core/rules`, no `packs/toy`).
- `test_build_settings_rejects_stale_embedded_submit_url` (new). Covers D4.
- `test_real_codex_truncation_matches_v8` (new, $0). Under the v9 config, a 60,000-character `exec_command` output
  reaches the model as 10,211 characters (10,215 bytes) containing `…50000 chars truncated…`. A model-supplied
  `max_output_tokens=20000` does not lift this cap. It equals v8's recorded maximum of about 10,216 bytes. This closes
  critic N2 and engineering tier-1 #12 for the no-`model_catalog_json` config.
- Probe changes:
  - `read_code_other`: in all three arms, 6 of 6 reads are denied (a file next to the tool that it does not import,
    `seed_notes.json`, `seed_rules.json`, `harness_core/rules.py`, `packs/toy/items.py`).
  - `read_marker`: the marker is listable, but its content is `Operation not permitted`.
  - The probe tool now imports a sibling helper the way `cli.py` does, and the probe no longer gets a `data` grant
    for the tool directory. The kit's own grants are enough.
- `test_build_kit_rejects`: added `:` in the workspace path, a code root inside the workspace, and a code root above
  the workspace.
- `test_verify_kit_detects_tampering`: added a granted code file edited after preparation.
- `test_client_policy_protects_kit`: the only plumbing path in the read line is `harness_core/tools/submit.py` (for
  mech-static), and a code grant inside the workspace is refused.
- The v8→v9 diff test gains the `hidden_added` set and the new read set.

**Checks outside pytest:**
- **Integration smoke** (scratchpad `integ.py`: mech-static, real S2 `cli.py`, S4 `calc.py`/`submit.py`, the tables
  index as `data`, run under `sandbox-exec`).
  - The code grant is `cli.py`, `otables.py`, `calc.py` and `submit.py`; the listable directory is `packs/officeqa`.
  - `find_tables "public debt" --years 1939-1940` returns table groups. `show_table --help`, `calc -c 'print(2+3)'`
    (prints 5) and `submit --help` all work with rc 0.
  - `head` of `seed_notes.json` and of `pack.py` gives `Operation not permitted`.
- **Escape attempts on the protected kit.** All denied:
  - case variants `.HARNESS/bin/x`, `.Harness/zdot/.zprofile`, `agents.md`, `Agents.MD`, `.ACTING-PREPARED.json`;
  - an actor-made symlink `h -> .harness` followed by `touch h/bin/y`;
  - `mv .harness/bin`, and creating `.harness/zdot/.zshenv`;
  - `os.rename('.HARNESS', …)`.
- `kit.py verify --deep`: 11,517 files, `tree_sha256 f7335cd6…`, 1.77 s wall. The report's 0.67 s is plausible with a
  warm page cache.

## 3. Defects

| # | Severity | Where (after the fix) | Defect | Status |
|---|---|---|---|---|
| D1 | **High (validity)** | `acting.py:178-187` `runtime_roots`, `:267-275` + read line in `client_policy`, `:579` `run_actor`; `kit.py:142-185` (`_import_closure`, `code_grants`), `:269-279` `build_kit` | The profile granted `(subpath packs/)` and `(subpath harness_core/)` to **every arm**. Details below the table. | **Fixed** |
| D2 | **Medium (validity)** | `acting.py:66-70` `HIDDEN`, `:317` profile line | The marker lives in the workspace and was readable. Details below the table. | **Fixed** |
| D3 | Low-Medium (reproducibility) | `kit.py:306-322` `verify_kit` | `verify_kit` hashed only `.harness/`, not the scripts the wrappers execute, so pack or tool code edited between preparation and the run went undetected. It now re-hashes every granted code file in `build_settings` (before the run) and for `kit_after` (after it). | **Fixed** |
| D4 | Low (silent failure) | `kit.py:302`; `acting.py:357-361` | The URL embedded in the `submit` wrapper overrides the environment. Details below the table. | **Fixed** |
| D5 | Low | `kit.py:55` `_SAFE_PATH` | `:` was allowed in kit paths. A workspace path containing `:` would split the kit PATH entry, so python3 and rg would silently fall back to the system versions. `:` is now refused. | **Fixed** |
| D6 | Low | `kit.py:236-239` | `code_roots` could be the workspace or one of its ancestors. That would allow actor-writable "code", or with D1 a whole-tree grant. Code roots must now be disjoint from the workspace. | **Fixed** |
| R1 | Low | `kit.py:438` `freeze_runtime` | If the prefix already exists without a manifest (an interrupted copy), it is hashed as-is and never compared with the source. The current copy is fine (checked above). Comparing trees would fail today on the source's `__pycache__` churn, so a fix should compare while excluding `__pycache__`. | Not fixed; the freeze is done |
| R2 | Info (report accuracy) | `S1-env-requests.md` item 12 | It says the design already keeps adapter content out of the code roots. It does not: the seed notes and rules live in `packs/officeqa/`. Items 1 and 10 (the `code_roots` + `data` advice) are superseded by `S1-env-review-requests.md`. | Superseded |

**D1 in detail.** The two `subpath` grants exposed:
- `packs/officeqa/seed_notes.json` (the text-arm seed knowledge) and `seed_rules.json` (the mech-arm seed rules);
- the rule engine and predicates;
- `packs/officeqa/{pack,rules,leak,observe,spec}.py`;
- the other packs.

As a result:
- `plain` could read and run the L1 tools, which breaks the "L1: all arms except plain" part of DESIGN §1.
- Text arms could read the mech arms' rules, and mech arms the text seed.

These are cross-arm channels outside the 16 KB budget. The paths are easy to find: every wrapper prints them
(`cat .harness/bin/find_tables`). I verified the leak inside the plain-arm sandbox: `wc -c` read both seed files.

**D1 fix.** `build_kit` records `meta['code'] = {files: {path: sha256}, dirs: [...]}`:
- `files` holds each wrapped script plus its sibling-import closure;
- `dirs` holds a directory, for listing only, when a script imports siblings (`cli.py` puts its own directory on
  `sys.path`).

`client_policy(code=marker['kit'])` grants exactly these as `literal`s. `runtime_roots` no longer contains the code
trees. An arm without wrappers (`plain`) reads no code at all.

**D2 in detail.** In v9 the marker holds the arm (`name`, `learner`, `seed`), the chosen note ids (`marker_extra`)
and the kit paths. Actors do read it: v8 actors printed the marker in **19 of 399 runs** (none 7, prompt 7, cards 5;
counted from command text only). The model would therefore see its own arm label in about 5% of episodes.

**D2 fix.** Added `(deny file-read-data (literal <ws>/.acting-prepared.json))`. The controller reads the marker from
outside the sandbox, and `ls` still lists it. Every other profile line is unchanged, as the diff test confirms.

**D4 in detail.** A `submit_url` given at preparation that differs from the endpoint port at run time would send every
submit to a port the sandbox denies. `meta['submit']['url']` is now recorded, and `build_settings` refuses a mismatch.

**Updated v8→v9 difference set** (`tests/test_v9_kit.py diff`, mech-static with the permissions key off; the test
asserts that this set is complete):

| Item | Value |
|---|---|
| Codex flags, other `-c` settings, network rule, all other profile lines | identical |
| env added | `TMPPREFIX`, `ZDOTDIR`; `HARNESS_SUBMIT_URL` on controller arms only |
| env changed | `PATH` |
| settings added | `include_permissions_instructions`, only when opted in |
| read removed | `<VENV>`, `<CODEX_RUNTIME_PYTHON>`, `<HARNESSADAPTER>`, `<CUA_NODE>` |
| read added | `<FROZEN_PYTHON>`, `<FROZEN_RG>`, `/private/var/select/sh`, plus the kit code files as literals: none for `plain`; `harness_core/tools/submit.py` for controller arms; for L1 arms, their tool scripts and closures (for example `cli.py`, `otables.py`, `calc.py`) and a listing of `packs/officeqa` |
| write-protected added | `.harness` |
| read-data denied added | `.acting-prepared.json` |

The `S1-env.md` §5 rows "profile read roots added … `<HARNESS_CORE>`, `<PACKS>`" and §7 gap 5 are superseded by D1.

**Interface changes in this review** (`contracts.py` untouched):
- `acting.client_policy(..., code=None)` and `client_command(..., code=None)` are new keywords. `run_actor` passes
  the marker's kit metadata.
- `runtime_roots()` no longer returns `harness_core/` or `packs/`.
- `kit.code_grants(meta)` is new, and kit metadata gains `code` and `submit.url`.
- `invocation.json` gains `code_read`.
- There are no other callers in the plumbing (checked with grep), so nothing else breaks.

## 4. Remaining risks (not fixed)

1. **Multi-agent is on for every arm** (`acting.py:64`, `V9_SETTINGS`: `agents.enabled` and `features.multi_agent`).
   This is v8 FULL behaviour, but in v9 sub-agent requests share the broker port with the main agent, and the S3 ledger
   and controller must attribute or refuse them. Decision for the user or S3; see request 10.
2. **`bash -l` PATH gap** (implementer gap 1). The kit PATH is lost only if the model chooses bash as its login shell.
   v8 used zsh for 21,507 of 21,507 commands.
3. **Model-visible false text remains.** `<environment_context>` still says the file system is unrestricted. Codex
   also sets `LANG`/`LC_ALL=C.UTF-8`, which overrides our `en_US.UTF-8`. Both are identical across arms.
4. **A tampered kit or edited code does not change the episode status.** It is only recorded in `kit_after`, and it
   is impossible from inside the sandbox. The audit must gate on it (request 9).
5. **The listing of `packs/officeqa/` shows file names** (for example `seed_notes.json`), though not their contents.
   Moving the actor-side CLI into its own directory removes even that (S2 request 2).
6. **Dynamic imports break.** A future pack tool that loads code or data dynamically (`importlib`, or files relative
   to `__file__`) will get `Operation not permitted`. The failure is loud, but it must be caught by the preflight
   probe before a campaign (S2 request 1).
7. **Not yet tested:** the broker (S3) in the loop, a real `submit` round-trip, compaction and splices. Truncation is
   now covered.
8. **`paths.CODEX` override.** It can still be changed through `OFFICEQA_CODEX` or `.task-paths.json`. The pinned
   default is correct, and `invocation.json` records the argv.
