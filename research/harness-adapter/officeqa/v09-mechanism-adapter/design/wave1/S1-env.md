# S1: L0 environment kit and sandbox (wave 1 report)

Stream S1 covers DESIGN.md §2 L0. The inputs were `understand/acting.md`, critic N2 and G5, and engineering review
#4 and #10. Every check below ran locally: no paid model call, and no network except 127.0.0.1. No reference answer or
grader was opened. Requests to other streams are in `S1-env-requests.md`.

**Terms used below.**
- **Seatbelt** is macOS's kernel sandbox. `sandbox-exec -p <profile>` runs a command under a rule file (the
  "profile"). A denied access returns `Operation not permitted`.
- **path_helper** is run by `/etc/zprofile` for every *login* zsh (`zsh -l`). It rebuilds PATH with the system and
  Homebrew directories first, which is why v8's `python3` and `rg` broke.
- **ZDOTDIR** is the directory where zsh looks for its startup files. The kit points it at a write-protected
  `.zprofile`, which runs after path_helper and restores PATH.
- **TMPPREFIX** is where zsh writes here-document temp files (a heredoc is `<<'PY' … PY`). zsh ignores TMPDIR, and
  the default `/tmp/zsh` is denied.
- **FTS5** is SQLite's full-text-search module.
- **Manifest** means a sha256 hash per file, so any later change to the frozen copy is detectable.

## 1. What was built

| File | What |
|---|---|
| `frameworks-v9/actor-python/` | Frozen copy (`rsync -a`) of `~/.cache/codex-runtimes/codex-primary-runtime/dependencies/python`: 11,517 files, 414.8 MB, 9 symlinks. Python 3.12.14, numpy 2.3.5, pandas 2.2.3, sqlite 3.53.1 with FTS5. `manifest.json` holds a sha256 per file, the symlink targets, the versions, `tree_sha256 f7335cd6…ee5af` and `python_sha256 ac60cfe0…8509e`. The manifest file's own sha is `40d6716f…0065`. The whole tree is `chmod a-w`, so no interpreter can add `__pycache__` files and break the manifest. |
| `frameworks-v9/bin/rg`, `rg.sha256` | Copy of `/Applications/ChatGPT.app/Contents/Resources/rg` (ripgrep 15.2.0). sha256 `05fe4a08…fd26`, the same as the source. It links only libiconv and libSystem. Read-only. |
| `officeqa-pro133-v9-plumbing/kit.py` (new) | The actor's tool kit and the frozen-runtime helpers. Functions: `build_kit`, `verify_kit`, `kit_hashes`, `tool_environment`, `readme_l0`, `actor_path`, `submit_url_for`, `validate_submit_url`, `freeze_runtime`, `verify_frozen`, `tree_manifest`, `runtime_versions`. CLI: `python kit.py freeze` and `python kit.py verify [--deep]`. |
| `officeqa-pro133-v9-plumbing/acting.py` | v9 launch path: kit environment, narrowed read roots, `.harness` write-protected, arm and kit re-checks, the `permissions_instructions` option, and a sandboxed `validate_config_offline`. Details in §3. |
| `officeqa-pro133-v9-plumbing/paths.py` | New settings: `FROZEN`, `ACTOR_PYTHON_PREFIX`, `ACTOR_PYTHON` (`bin/python3.12`), `ACTOR_PYTHON_MANIFEST`, `ACTOR_RG`, `ACTOR_RG_SHA256_FILE`, `HARNESS_CORE`, `PACKS`. Each can be overridden in `.task-paths.json` (keys `frozen_runtime`, `actor_python_prefix`, `actor_rg`). The Codex default is now the pinned `frameworks-v8/codex-0.155.0-alpha.9.2/codex`, ahead of the app-bundled binary. |
| `tests/test_v9_kit.py` (new) | 27 tests (§4), plus a CLI: `make SCRATCH ARM` (used by the probe script) and `diff` (prints the table in §5). |
| `tests/probe_kit.sh` (new) | Tier-0 probe runner. Builds a scratch episode exactly as `run_actor` would, then runs each probe as `env -i <actor env> /usr/bin/sandbox-exec -p <v9 profile> /bin/zsh -lc\|-c <cmd>` from the workspace, with a pipe as stdout. |

**Kit layout.** `<ws>/.harness/` is built before the marker is written, and its hashes are recorded in
`.acting-prepared.json` under `kit`.
- `bin/python` and `bin/python3` are symlinks to the frozen `python3.12`. `bin/rg` is a symlink to the frozen rg.
- Each L1 tool gets one `#!/bin/sh` wrapper: `exec <frozen python3.12> -I -B <script> [fixed args] "$@"`.
- Controller arms also get `bin/submit`, which runs `harness_core/tools/submit.py` (owned by S4).
- `zdot/.zprofile` holds `export PATH=<ws>/.harness/bin:/usr/bin:/bin:/usr/sbin:/sbin`.
- Wrappers are mode 0555 and `.zprofile` is 0444. The real guard is the Seatbelt rule
  `deny file-write* (subpath <ws>/.harness)`.
- `build_kit` refuses bad input:
  - tool wrappers for arms without L1;
  - reserved or invalid tool names;
  - scripts outside the code roots, or scripts that are not canonical regular files;
  - fixed arguments that are not plain tokens;
  - a submit URL on a non-controller arm, or a submit URL that is not `http://127.0.0.1:PORT/v1/submit`;
  - a second build in the same workspace;
  - non-canonical workspaces;
  - paths with characters unsafe for sh or Seatbelt text.

**Environment.** `kit.tool_environment(ws, arm, extra)` returns:
- the v8 keys `HOME`, `TMPDIR`, `LANG`, `PYTHONNOUSERSITE`, `PYTHONDONTWRITEBYTECODE` and `MPLCONFIGDIR`;
- plus `TMPPREFIX=<ws>/.actor-tmp/zsh`, `ZDOTDIR=<ws>/.harness/zdot` and `PATH=<ws>/.harness/bin:/usr/bin:/bin:/usr/sbin:/sbin`;
- plus `extra`.

`extra` must contain `HARNESS_SUBMIT_URL` for controller arms and must not contain it for other arms. It cannot
override a base key, and credential-like names are refused. `acting.actor_env_extra` derives the submit URL from the
actor's own broker endpoint.

**README.** `readme_l0(arm, corpus, index)` returns the same text for every arm (a test checks this). It says:
- python3 (numpy, pandas, sqlite3 FTS5) and rg are available, and heredocs work;
- where the read-only corpus and index are;
- only the workspace is writable, and `.harness/`, AGENTS.md and README.md are read-only;
- everything else is denied (`Operation not permitted`: home, other tasks, /tmp, /opt/homebrew) and there is no
  internet, so do not search the disk.

## 2. How it was verified (exact commands and results)

Python = `/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/adapter-comparison-work/.venv/bin/python`
(3.12.14, pytest 9.1.1). Working directory = `officeqa-pro133-v9-plumbing/`.

```
$PY -B kit.py freeze          # once: manifest + rg.sha256 + chmod a-w; versions {python 3.12.14, numpy 2.3.5, pandas 2.2.3, sqlite 3.53.1, fts5 true}
$PY -B kit.py verify --deep   # re-hashes all 11,517 files: OK in 0.67 s
$PY -B -m pytest -q -p no:cacheprovider tests/test_v9_kit.py
...........................                                              [100%]
27 passed in 7.77s
/bin/sh tests/probe_kit.sh <scratch> plain|text-static|mech-static     # exit 0 for all three
$PY -B tests/test_v9_kit.py diff                                       # the table in §5
```

No test takes more than 60 s (the whole file takes about 8 s), so no test is marked slow.

### 2.1 Tier-0 probe outputs

These are the mech-static outputs under the v9 profile. `<S>` is the scratch dir, `<P>` the project dir and `~`
`/Users/albertzhao`. The actor environment comes from `build_settings`, and the profile from `acting.client_policy`.

| # | Probe (shell) | Output | Pass |
|---|---|---|---|
| 1–2 | `print -r -- "$PATH"` (`-lc`, `-c`) | `<S>/ws/.harness/bin:/usr/bin:/bin:/usr/sbin:/sbin` in both | ✓ PATH order survives path_helper |
| 3 | `whence -p python python3 rg` | `<S>/ws/.harness/bin/{python,python3,rg}` | ✓ |
| 4 | `python3 --version` | `Python 3.12.14` | ✓ (v8: Xcode stub error) |
| 5–6 | `python3 -c 'import sys, sqlite3, numpy, pandas; … fts5 …'` (`-lc`, `-c`) | `{"python": "3.12.14", "prefix": "<P>/frameworks-v9/actor-python", "numpy": "2.3.5", "pandas": "2.2.3", "fts5": true, "foreign": []}` | ✓ nothing on sys.path from `codex-runtimes` |
| 7–8 | `rg -c -i treasury <P>/opus-officeqa-133/corpus/treasury_bulletin_1939_01.txt` (`-lc`, `-c`) | `58` | ✓ (v8: `operation not permitted: rg`) |
| 9 | `rg -l -i 'public debt' <corpus> \| wc -l` | `697` | ✓ |
| 10 | `python3 - <<'PY' … PY` and `cat <<'X' … X` | `heredoc-ok 42`, `cat-heredoc-ok` | ✓ (v8: `can't create temp file for here document`) |
| 11 | `$TMPPREFIX\|$ZDOTDIR\|$HOME\|$TMPDIR` | `<S>/ws/.actor-tmp/zsh\|<S>/ws/.harness/zdot\|<S>/ws/.actor-home\|<S>/ws/.actor-tmp` | ✓ |
| 12 | `echo ok > note.txt && cat note.txt` | `ok` | ✓ the workspace is writable |
| 13 | `: > /private/tmp/probe-kit-outside`, `: > <S>/probe-kit-outside` | `zsh:1: operation not permitted: …` twice | ✓ (files absent afterwards) |
| 14 | `ls ~/Documents` | `ls: ~/Documents: Operation not permitted` | ✓ |
| 15 | `ls <P>/officeqa-pro133-none-v8-work/runs` | `Operation not permitted` | ✓ |
| 16 | `ls ~/.cache/codex-runtimes/codex-primary-runtime/dependencies/python` | `Operation not permitted` | ✓ so probes 4–6 prove the frozen copy does not depend on the original |
| 17–20 | `ls` of the controller venv, `frameworks-v8/next-a48c0f9/harnessadapter`, `cua_node`, `/opt/homebrew/bin` | `Operation not permitted` ×4 | ✓ |
| 21 | python socket to `1.1.1.1:443` (2 s timeout) | `blocked PermissionError 1` | ✓ |
| 22 | python socket to `127.0.0.1:22` (not the broker port) | `blocked PermissionError 1` | ✓ |
| 23 | touch, append, rm, chmod, mv, hard link, symlink, `rm -rf` on `.harness`, then append to `AGENTS.md` | 9× `Operation not permitted`, 9× `rc=1` | ✓ `verify_kit` passes afterwards; hard links to protected files are denied too |
| 24 | `sh -c 'echo sh-ok'` | `sh-ok` | ✓ (without the new `/private/var/select/sh` read, every `sh` printed `Error opening /private/var/select/sh`) |
| 25 | `probe_tool alpha 'b c'` (wrapper `[script, 'sub']`) | `{"argv": ["sub","alpha","b c"], "isolated": 1, "no_bytecode": true, "executable": "<P>/frameworks-v9/actor-python/bin/python3.12", "numpy": "2.3.5", "submit_url": "http://127.0.0.1:1/v1/submit"}` | ✓ |
| 26 | `whence -p submit; echo $HARNESS_SUBMIT_URL` | `<S>/ws/.harness/bin/submit`, `http://127.0.0.1:1/v1/submit` | ✓ |
| 27 | `/bin/bash -lc 'echo $PATH; command -v python3'` (informational) | `/usr/local/bin:/System/Cryptexes/…:/opt/homebrew/bin:<kit>/bin…`, `/usr/bin/python3` | ✗ known gap: bash login shells still get path_helper's order (§7) |

The other arms differ only where expected:
- **plain:** probe 25 prints `none` (no L1 wrappers) and probe 26 prints `none` / `unset`.
- **text-static:** probe 25 is the same as mech-static except `"submit_url": null`, and probe 26 prints `none` / `unset`.

After normalising paths, probes 1–22 and 24 produce byte-identical output for plain, text-static and mech-static, so
L0 parity holds. Probe 23 is excluded only because its `rm -rf` listing names each arm's own wrappers.

### 2.2 Codex offline config validation

`validate_config_offline` runs `app-server --strict-config` and a `config/read`, with no model call. It was run on the
v9 mech-static settings in four combinations:

| | outside sandbox | inside the v9 profile (`policy=`, `home=<ws>/.client-validate-N`) |
|---|---|---|
| default (key absent) | passed; `include_permissions_instructions: true` | passed; `true` |
| `permissions_instructions=False` | passed; the key is accepted and echoed as `false` | passed; `false` |

In all four, `config/read` echoes `shell_environment_policy.set` equal to the kit environment,
`model_context_window 1048576` and `sandbox_mode danger-full-access`.

The first sandboxed attempt failed (`Operation not permitted (os error 1)`) because the server inherited an unreadable
cwd. `validate_config_offline` now uses `cwd=home` when a home is given.

### 2.3 Real pinned Codex inside the v9 profile ($0, 127.0.0.1 only)

This is an addition beyond the stream brief. It is an early answer to critic G5 and engineering #4, and it decides the
`include_permissions_instructions` question.

**Setup.** `test_real_codex_exec_in_v9_sandbox` runs `acting.run_actor` end to end. That means the real
`codex exec` 0.155.0-alpha.9.2, the v9 argv, the v9 Seatbelt profile and final.md collection. The endpoint is a local
fake Responses server (a scripted SSE stream: `exec_command(<probe>)`, then a final message). It runs three episodes:
plain, plain with `permissions_instructions=False`, and mech-static.

**Results.**
- Codex starts and completes (`status completed`, final.md valid, `kit_after.intact`, exactly 2 `POST /v1/responses`)
  **without the venv, harnessadapter or cua_node** in the read roots.
- Codex's own `exec_command` runs `/bin/zsh -lc`. Its output shows:
  - `PATH=<ws>/.harness/bin:/usr/bin:/bin:/usr/sbin:/sbin`;
  - python3 and rg resolved from the kit, prefix = the frozen python, numpy 2.3.5;
  - an rg count line, `heredoc-ok`;
  - `HARNESS_SUBMIT_URL` set only in the mech-static episode (as `<fake endpoint>/submit`), and `bin/submit` present
    only there.
- Codex adds its own variables to the shell environment: `CODEX_CI`, `CODEX_SESSION_ID`, `CODEX_THREAD_ID`,
  `CODEX_VERSION`, `COLORTERM`, `GH_PAGER`, `GIT_PAGER`, `PAGER=cat`, `NO_COLOR=1`, `TERM=dumb`, `LOGNAME=root`, and
  **`LANG`/`LC_ALL`/`LC_CTYPE=C.UTF-8`, which overrides our `LANG=en_US.UTF-8`**. No `*_API_KEY`, `VIRTUAL_ENV` or
  `PYTHONPATH` appears.
- **`include_permissions_instructions=false` removes exactly one input part:** the 362-byte developer text
  `<permissions instructions> … No filesystem sandboxing - all commands are permitted. Network access is enabled. …`.
  After normalising paths, every other input part is identical, and so is every other request field (instructions,
  tools, reasoning, include, …). Only the per-run ids in `client_metadata` and `prompt_cache_key` differ.
- `<environment_context>` still contains `<permission_profile type="disabled"><file_system type="unrestricted" />` (§7).
- The known fallback-metadata warning appears as in v8. Truncation and compaction were not exercised here.

### 2.4 Integration smoke with the other streams' current files (informational, not in the tests)

Setup:
- A mech-static kit was built with the real `packs/officeqa/cli.py` (S2) as `[cli.py, 'find_tables'|'show_table'|'printings']`.
- `harness_core/tools/calc.py` and `submit.py` (S4) were added as wrappers.
- `data=(frameworks-v9/officeqa-tables-index,)` and `env_extra={OFFICEQA_CORPUS, TABLES_INDEX}` were passed.

Inside the sandbox:
- `find_tables "public debt" --years 1939-1940 --limit 3` printed table groups with `file:line` anchors (rc 0).
- `calc --help` and `submit --help` printed their usage.

## 3. acting.py: what changed (everything else is v8)

- **`prepare_actor_workspace(ws, adapter=None, *, common_instructions, arm, tool_wrappers=None, submit_url=None, marker_extra=None, code_roots=None)`.**
  - `arm` is required.
  - It calls `kit.build_kit` after `.actor-home` and `.actor-tmp` are created.
  - The marker gains `contract`, `arm`, `kit` (the full hash metadata) and `extra`.
  - v9 arms pass `adapter=None` and the rendered AGENTS.md text.
- **`runtime_roots(*, server=False, data=())`** returns the frozen python prefix, the Codex binary, the frozen rg,
  `harness_core/`, `packs/`, and the data roots. The venv, `sys.base_prefix`, harnessadapter and cua_node are added
  only when `server=True`, which `run_actor` sets only when the marker's deployment has an MCP server or roles; that
  never happens for v9 arms. Missing roots raise an error.
- **`client_policy(…, server=False, data=())`.**
  - Read roots come from `runtime_roots`.
  - The literal read `/private/var/select/sh` is added (the `/bin/sh` trampoline).
  - `PROTECTED` adds `.harness`.
  - Everything else in the profile is byte-identical to v8 after normalisation (a test checks this).
- **`build_settings(…, *, endpoint, arm, env_extra=None, data=(), permissions_instructions=True)`.**
  - Re-checks that the marker's arm equals `arm`, that the kit is intact (`kit.verify_kit`) and that the frozen runtime
    is intact (`kit.verify_frozen`, shallow).
  - The shell environment is `kit.tool_environment` plus the derived submit URL.
  - `V9_SETTINGS`, the v8 FULL values (`agents.enabled`, `features.multi_agent`, 1,048,576 / 800,000), now applies to
    every arm instead of being chosen by the profile name.
  - `include_permissions_instructions=false` is added only when `permissions_instructions=False`. The default stays
    off, as the brief asked, but §2.3 has now verified it.
- **`run_actor(…, *, endpoint, arm, env_extra=None, data=(), permissions_instructions=True)`.**
  - `invocation.json` gains `arm`, `contract`, `kit_sha256`, `frozen_runtime`, `data_roots`, `read_roots`,
    `permissions_instructions` and `effective_auto_compact_limit_observed: 244800`.
  - `feedback_source` names the broker controller text for controller arms.
  - After the run, `result.json` gains `kit_after {intact, kit_sha256|error}`.
- **`validate_config_offline(settings, *, timeout=25, home=None, policy=None)`** can run under a Seatbelt profile.
- **Unchanged:** workspace guards (canonical, empty, separate from adapter, records and inputs), the marker/AGENTS.md
  sha check, `collect_final` (`O_NOFOLLOW`, regular file, ≤1 MiB, equal to the last event message), `_stop_group`,
  `parse_events`, `command_for` flags, `client_environment`, DISABLED_FEATURES and the provider block.

## 4. Tests (`tests/test_v9_kit.py`, 27)

| Tests | What they check |
|---|---|
| Frozen runtime (3) | Deep manifest check and versions; the tree is read-only; the frozen python's `sys.prefix` is its own and nothing on `sys.path` or `otool -L` references `codex-runtimes`; the rg sha and version. |
| `build_kit` (5 arms + 3) | Layout per arm (submit only for controller arms, wrappers only for L1 arms, `[script, arg]` form); embedded submit URL; 18 rejection cases. |
| `verify_kit` | Tampering detected: a wrapper rewritten, the rg symlink retargeted, an extra file, a mode change, a deleted file, `.harness` swapped for a symlink. |
| Environment and README | Exact environment; submit-URL rules; reserved and credential-like names; README identical for all 5 arms. |
| acting (5) | Narrowed roots, and the server-only additions; marker content; settings with and without the permissions key; arm mismatch; kit tamper; the profile protects `.harness`. |
| `test_probe_kit_sh` (3 arms) | Runs `probe_kit.sh` and checks every probe (§2.1); afterwards the kit and AGENTS.md are intact and nothing was written outside. |
| `test_validate_config_offline` (2) | §2.2, outside and inside the sandbox. |
| `test_real_codex_exec_in_v9_sandbox` | §2.3. |
| `test_v8_v9_differences_are_the_documented_ones` (3) | §5 set equality against the recorded v8 `invocation.json` (none root, Q001; read-only). |

## 5. v8 → v9 differences (argv / profile / env)

The v8 side is `officeqa-pro133-none-v8-work/runs/baseline/001/records/invocation.json`, normalised with `<WS>`,
`<CORPUS>`, `<INDEX>`, the same port, and placeholders for the runtime paths. The test asserts that this is the complete
list of differences.

| Item | v8 | v9 | Why |
|---|---|---|---|
| Codex flags (`--strict-config … exec --ignore-user-config --ignore-rules --skip-git-repo-check --ephemeral --json --cd <WS> --output-last-message <WS>/final.md … -`) | as shown | identical | — |
| `-c` settings other than the shell env | — | identical values, including model, provider block, `sandbox_mode=danger-full-access`, 1,048,576 / 800,000, `agents.enabled=true`, `features.multi_agent=true`, `features.skip_host_skill_discovery=true`, 32 other `features.*=false`, trust | "Codex config: as v8" (DESIGN §2); now fixed for all arms |
| `-c include_permissions_instructions` | absent | absent by default; `false` when `permissions_instructions=False` | removes the false "no sandbox / network enabled" text (verified §2.3) |
| env `PATH` | `<VENV>/bin:<CUA_NODE>/bin:/usr/bin:/bin:/usr/sbin:/sbin` | `<WS>/.harness/bin:/usr/bin:/bin:/usr/sbin:/sbin` | kit first; no controller venv or node |
| env `TMPPREFIX` | – | `<WS>/.actor-tmp/zsh` | zsh heredocs |
| env `ZDOTDIR` | – | `<WS>/.harness/zdot` | the `.zprofile` that runs after path_helper |
| env `HARNESS_SUBMIT_URL` | – | `http://127.0.0.1:<port>/v1/submit` (controller arms only) | L4 `submit` (contracts `SUBMIT_ENV`) |
| env `OFFICEQA_*` / `TABLES_INDEX` | – | only if orchestration passes `env_extra` | L1 tool paths |
| env HOME, TMPDIR, LANG, PYTHONNOUSERSITE, PYTHONDONTWRITEBYTECODE, MPLCONFIGDIR | — | identical | — |
| profile read roots removed | `<VENV>` (controller venv), `<CODEX_RUNTIME_PYTHON>` (`sys.base_prefix`), `<HARNESSADAPTER>` (framework package), `<CUA_NODE>` | – | not needed; Codex starts without them (§2.3) |
| profile read roots added | – | `<FROZEN_PYTHON>` (`frameworks-v9/actor-python`), `<FROZEN_RG>` (`frameworks-v9/bin/rg`), `<HARNESS_CORE>`, `<PACKS>` (read-only code the wrappers run), `/private/var/select/sh` (literal), plus `data` roots (the tables index) | kit and L1 |
| profile metadata literals | parents of the workspace and read roots | the same rule; the set follows the roots | derived |
| profile `deny file-write*` | AGENTS.md, README.md, officeqa.py, .codex, .acting-prepared.json, .ha-demo-generated.json | the same plus `.harness` | kit write-protected |
| profile network | `(remote tcp "localhost:<port>")` only | identical | — |
| all other profile lines (deny default, process, mach, devices, pty, workspace grant) | — | identical | — |
| Codex binary | `frameworks-v8/codex-0.155.0-alpha.9.2/codex` | the same (now also the `paths.py` default) | — |

## 6. Interface deviations from contracts.py

`contracts.py` is unchanged and nothing in it needs to change. The seams used are `ArmSpec` (and `validate()`),
`STANDARD_ARMS`, `SUBMIT_ENV`, `SUBMIT_PATH` and `CONTRACT_VERSION`. Deviations from, or additions to, the brief:

1. The wrappers run `python -I -B` rather than `-I`. `-B` keeps bytecode out of `packs/` and `harness_core/`. `-I`
   already ignores `PYTHONDONTWRITEBYTECODE`.
2. A `tool_wrappers` value may be `[script, *fixed args]` as well as a script path. S2's `packs/officeqa/cli.py` takes
   the tool name as its first argument.
3. `build_kit` has keyword-only extras: `python`, `rg` and `code_roots`. `prepare_actor_workspace` forwards `code_roots`.
4. `readme_l0` ignores `arm` apart from validating it, so L0 is arm-independent. Pack and tool usage is left to
   orchestration.
5. `acting` derives `HARNESS_SUBMIT_URL` from the endpoint. The broker must therefore serve `/v1/submit` on the actor's
   own port (request 11).
6. `arm` is a required keyword on `prepare_actor_workspace`, `build_settings` and `run_actor`. Legacy call sites in
   campaign, cards_learning, preflight and run raise `TypeError` until they are updated (request 1).
7. The profile gained a read of `/private/var/select/sh`, which was not in the brief. It is needed for quiet `#!/bin/sh`
   wrappers.
8. The tables index was written by another stream to `frameworks-v9/officeqa-tables-index/`. It is not covered by the
   S1 manifest.

## 7. Known gaps

1. **Bash login shells** (`bash -lc`, or the model passing `shell: bash`) still get path_helper's PATH, so `python3`
   resolves to the Xcode stub (probe 27).
   - v8 used zsh for 21,507 of 21,507 commands.
   - Fix if wanted: a write-protected `.actor-home/.bash_profile` that restores PATH. That file is outside the kit
     scope, so it was not done.
2. **Codex overrides some shell variables** (§2.3): `LANG`/`LC_ALL=C.UTF-8`, `LOGNAME=root`, `TERM=dumb`, and it adds
   `CODEX_*` ids. This is harmless, and it is identical for all arms.
3. **A false hint remains in `<environment_context>`**: `<permission_profile type="disabled"><file_system type="unrestricted" />`,
   even with the permissions key off. `include_environment_context=false` would also drop the cwd, date and shell.
   That needs a decision, so it was not changed.
4. **rg prints nothing when its stdout is a regular file outside the readable roots.** This is ripgrep's own-output
   guard. Codex uses pipes or a PTY and the probe script uses a pipe, so episodes are not affected.
5. **The actor can read `harness_core/` and `packs/` in every arm** (the rule engine, predicates, S2's tools and any
   `__pycache__`). This keeps parity across arms but is a text channel outside the 16 KB budget (request 12).
6. **A tampered kit is recorded, not enforced.** `result.json kit_after` records it but does not change the episode
   status. The audit must gate on it (request 6).
7. **A half-prepared workspace can be left behind** if `build_kit` rejects its inputs, as v8 left on other preparation
   errors. Episode directories are single-use, so it is never reused.
8. **Not yet tested:** compaction, truncation size, the broker (S3) in the loop, the controller splices and a real
   `submit` round-trip. The real-Codex test talks to the fake upstream directly. This is wave-3 tier 1.
9. **The frozen tree is read-only.** Deleting or refreshing it needs `chmod -R u+w`. `freeze_runtime` refuses to
   overwrite an existing manifest.
10. **The tests use local absolute paths**: the opus corpus and the recorded v8 invocation. The affected tests skip if
    these are missing.
