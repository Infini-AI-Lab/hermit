# S1 (L0 kit + sandbox) — requests to other streams

S1 edited none of these files. Each item says who owns the change and why.

## S6: orchestration (campaign.py, preflight.py, run.py, experiment.py, full_audit.py)

1. **Update the call sites. v9 acting needs an explicit `arm`.** A call without `arm=` raises `TypeError`, on purpose,
   so a v9 episode can never fall back to the broken v8 environment. The calls that will fail until they are updated:
   `campaign.py:78` (`prepare_actor_workspace`), `campaign.py:127` (`run_actor`), `cards_learning.py:407`
   (`run_actor`; owned by S5), `preflight.py:81` (`build_settings`), `run.py:41` (`run_actor`).
   ```python
   cli = str(PACKS / 'officeqa/cli.py')                      # S2's multi-command CLI takes the tool name first
   tools = {t: [cli, t] for t in ('find_tables', 'show_table', 'printings')}
   tools['calc'] = str(HARNESS_CORE / 'tools/calc.py')         # S4
   acting.prepare_actor_workspace(ws, None, common_instructions=AGENTS_TEXT, arm=arm,
                                  tool_wrappers=tools if arm.l1_tools else {},
                                  marker_extra={'notes': [...], 'adapter_revision': ...})
   (ws / 'README.md').write_text(kit.readme_l0(arm, CORPUS, INDEX) + pack_usage_text)
   acting.run_actor(ws, records, prompt, CORPUS, INDEX, timeout=..., endpoint=relay.endpoint, arm=arm,
                    env_extra={'OFFICEQA_CORPUS': str(CORPUS), 'TABLES_INDEX': str(TABLES_DIR / 'tables.sqlite')},
                    data=(TABLES_DIR,), permissions_instructions=False)   # see item 5
   ```
   - This exact combination was smoke-tested inside the v9 sandbox: `find_tables`, `calc --help` and
     `submit --help` all work (S1-env.md §2.4).
   - v9 arms pass `adapter=None`. The rendered AGENTS.md text (common text plus the chosen notes) goes in
     `common_instructions`. The actor never gets read access to the adapter.
   - `HARNESS_SUBMIT_URL` is derived from the endpoint for controller arms. Do not pass it yourself. If you do, it must
     be the same value.
   - Env names are refused if they contain KEY, TOKEN, SECRET, PASSWORD, PASSWD, AUTH, CREDENTIAL, COOKIE or SESSION.
     Values must be one line.
2. **The tables index is a data root.** Another stream wrote it to `frameworks-v9/officeqa-tables-index/tables.sqlite`
   (read-only, 147 MB). It is not part of S1's manifest. Pass it as `data=(path,)`, or pass its directory. Otherwise
   the sandbox denies it.
3. **freeze() / verify_freeze().**
   - Record `kit.verify_frozen(deep=True)`. It returns `manifest_sha256`, `tree_sha256`, `python_sha256`, the versions
     and `rg_sha256`, and takes 0.7 s.
   - Compare these values on resume.
   - Every `invocation.json` already carries `frozen_runtime`, from a shallow check.
   - Optional `.task-paths.json` keys: `frozen_runtime`, `actor_python_prefix`, `actor_rg` and `codex`. The Codex
     default is now the pinned `frameworks-v8/codex-0.155.0-alpha.9.2/codex`.
4. **experiment.py.** `acting` still imports `FULL`, `MWM` and `PROJECT_DOC_MAX_BYTES`. Keep those names, or tell S1.
   v9 Codex settings no longer depend on `FULL`: `acting.V9_SETTINGS` is applied to every arm.
5. **Decide the `permissions_instructions` default.** The default is still `True` (Codex keeps its text), as the stream
   brief asked.
   - The $0 real-Codex test (`test_real_codex_exec_in_v9_sandbox`) shows that `false` removes exactly one input part:
     the 362-byte `<permissions instructions>` developer text ("No filesystem sandboxing … Network access is enabled").
     Nothing else in request 001 changes.
   - DESIGN §2 L0 makes that test the condition for turning it on.
   - Recommendation: pass `permissions_instructions=False` for all arms and record it in PREREG.
6. **full_audit.**
   - Fail an episode whose `result.json` has `kit_after.intact == false`. The kit is re-hashed after every run. A
     tampered kit is recorded, but the episode status is not changed.
   - Check that `invocation.frozen_runtime` equals the freeze record.
   - Check that `invocation.arm` equals the root's arm.
7. **preflight.** Add the tier-0 L0 check. Either run
   `adapter-comparison-work/.venv/bin/python -m pytest -q tests/test_v9_kit.py` (27 tests, about 8 s), or run
   `tests/probe_kit.sh` and parse it with the test module. The v8 check `frameworks_are_private_copies` should also
   accept `frameworks-v9`.

## S4: harness_core/tools/submit.py and the sandboxed compute runner

8. **How the kit runs `submit`.**
   - The kit wrapper runs `exec <frameworks-v9/actor-python/bin/python3.12> -I -B <plumbing>/harness_core/tools/submit.py "$@"`.
   - `HARNESS_SUBMIT_URL` is in the environment for controller arms. It is also embedded in the wrapper when
     orchestration passes `submit_url`.
   - `-I` means the script's own directory is not on `sys.path` and `PYTHON*` variables are ignored. Insert the plumbing
     root yourself if you import `harness_core`, and stay stdlib-only.
   - The script can write only inside the workspace. Its only network is TCP to the broker port.
9. **Compute runner.** The frozen interpreter is `paths.ACTOR_PYTHON` (numpy 2.3.5, pandas 2.2.3). Its prefix is
   read-only (`chmod a-w`), so running it cannot add `__pycache__` files, and the manifest stays valid. Read access to
   `paths.ACTOR_PYTHON_PREFIX` is enough to run it under `sandbox-exec`.

## S2: pack tools

10. **How the kit runs pack tools.**
    - Pack CLIs run the same way: `python3.12 -I -B <script> [fixed args] "$@"`. The fixed args are given as
      `tool_wrappers={name: [script, *args]}`. `cli.py` works as `[cli.py, 'show_table']`, so no per-tool scripts are
      needed.
    - The script must be a canonical regular file under `packs/` or `harness_core/`, or under a `code_roots` entry that
      is also passed as `data`.
    - Open sqlite read-only with `file:<path>?mode=ro&immutable=1`. The index directory is not writable, so no
      `-wal`/`-shm`/journal file can be created.
    - Tool names must match `[a-z][a-z0-9_-]{0,31}`. `python`, `python3`, `rg` and `submit` are reserved.

## S3: broker

11. **Serve `/v1/submit` on the actor's own port.** Serve `SUBMIT_PATH` (`/v1/submit`) on the same `127.0.0.1:<port>`
    as `/v1/responses`. The Seatbelt profile allows TCP to that one port only, and `acting` derives
    `HARNESS_SUBMIT_URL = http://127.0.0.1:<port>/v1/submit` from the actor endpoint.

## Orchestrator / user decision

12. **The actor can read the code roots.** The actor has read access to `harness_core/` and `packs/`, the same for
    every arm. It can therefore read the rule engine, the predicates and any seed rule written as pack code. That is a
    model-visible channel outside the 16 KB text budget. Either:
    - keep adapter content (rules, notes, policy) out of these directories, which the design already does; or
    - narrow the grant to the directories the wrappers import from, via `code_roots` + `data`, once S2/S4 have fixed
      their layouts.

No change to `harness_core/contracts.py` is needed.
