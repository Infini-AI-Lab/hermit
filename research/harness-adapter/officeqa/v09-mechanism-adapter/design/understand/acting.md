# How an actor episode is set up and launched (v8), and the smallest L0 changes for v9

Scope: `officeqa-pro133-v8-plumbing/{acting,campaign,experiment,paths,officeqa_tools,full_runtime,full_sandbox,run}.py`,
`deployment/`, the framework host code these files call (`frameworks-v8/*/harnessadapter/hosts/*.py`), and the
records of the three finished v8 run roots (399 episodes). Below, `PLUMB/` means
`officeqa-pro133-v8-plumbing/` and `FW/` means `frameworks-v8/next-a48c0f9/harnessadapter/`.

How the claims were checked. I read the code. I scanned all 399 `records/events.jsonl` and the last `api/NNN/request.json`
of each episode. I also reproduced the actor shell locally. The recorded v8 Seatbelt profile (the macOS
`sandbox-exec` rule file) was taken from `runs/baseline/001/records/invocation.json`, only its workspace path was
changed to a scratch directory, and `/bin/zsh -lc` was run inside it with the exact v8 tool environment. That
reproduction made no model calls, installed nothing and wrote only to scratch. The scripts are in
`understand/acting_repro/` (`mk.py` + `probe.sh` = v8, `mk9.py` + `probe9.sh` = v9 candidate).

---

## 0. Summary

1. **All three arms get the same launch.** After normalising the root path, the broker port and the framework
   directory, the Codex argv and the sandbox profile are identical across none, prompt and cards. The arm changes
   only `AGENTS.md`, the adapter path recorded in the marker, and which `harnessadapter` package is readable
   (prompt used `frameworks-v8/prompt-c0c9b87-v6f`).
2. **Every actor command ran as `/bin/zsh -lc` (21,507 of 21,507).** Because `-l` makes zsh a login shell, it runs
   `/etc/zprofile`, and that runs `path_helper`. `path_helper` rebuilds PATH as
   `/usr/local/bin:…:/usr/bin:…:/opt/homebrew/bin:<venv>/bin:<cua_node>/bin`, which pushes the harness's PATH entries
   to the end. That one reordering causes both the python3 failure and the rg failure.
   - `python3` now resolves to `/usr/bin/python3`, the Xcode command-line-tools stub. The stub needs
     `/Library/Developer` (not readable in the sandbox) and a cache in `/var/folders` (not writable), so it prints
     `xcode-select: error: No developer tools were found`.
   - `rg` now resolves to `/opt/homebrew/bin/rg`, which is outside the read roots. zsh prints
     `operation not permitted: rg`. That is EPERM, not "command not found": the file exists, but the sandbox denies it.
3. **Heredocs fail because zsh writes here-document temp files to `$TMPPREFIX`**, which defaults to `/tmp/zsh`. zsh does
   not use `$TMPDIR`. The sandbox denies writes to `/tmp`, so every `python - <<'PY'` failed. In the none arm, 165 of
   166 heredoc commands failed.
4. **The actor's `python` is the controller's venv** (`adapter-comparison-work/.venv`, Python 3.12.14,
   `include-system-site-packages = false`). That venv has no numpy. Its *base* interpreter,
   `~/.cache/codex-runtimes/codex-primary-runtime/dependencies/python`, does have numpy 2.3.5 and pandas 2.2.3, SQLite
   FTS5 works in it, and it is already a read root. A shim that points at it fixes numpy with no install; this was
   verified inside the sandbox. Homebrew `python3.11` also has numpy (2.4.6), but it needs four more Homebrew library
   roots (sqlite, openssl@3, xz, mpdecimal), and its site-packages also holds torch, sympy and matplotlib. I do not
   recommend it.
5. **The read scope is already narrow at file level.** The actor tried 205 out-of-scope listings in 37 runs (for
   example `find /Users`, `mdfind`, `ls ..`). Every one returned `Operation not permitted` or empty output, and no
   command output in 399 episodes contains another episode's run path. What *is* wrong is that Codex tells the model
   `"No filesystem sandboxing … Network access is enabled"` (a developer message in every request), which invites
   those searches. Needlessly readable today: the controller venv, the whole framework `harnessadapter/` package and
   `cua_node`.
6. **Tool output is truncated to at most 10,226 bytes, head plus tail, whatever the model asks for.** In the none arm,
   464 of 2,779 tool outputs visible in the final requests were truncated, even though the model asked for
   `max_output_tokens` of 20k–50k. Codex logs `Model metadata for glm-5p3-flash not found. Defaulting to fallback
   metadata` in 399 of 399 runs. The truncation is not silent: Codex adds a `Warning: truncated output` header and a
   `…N chars truncated…` marker. But the middle rows are lost.
7. **Timeout means an empty answer.** 13 of 399 runs hit the 3,600 s wall clock (none 4, prompt 5, cards 4). All 13 got
   `answer: ''` because Codex writes `final.md` only when the turn completes. 29 runs ran longer than 1,800 s.

---

## 1. How the workspace is created and what goes into it

### 1.1 Call chain

`campaign.run` (`PLUMB/campaign.py:363-376`) → `actor(key, arm, ordinal, task, adapter)`
(`campaign.py:114-167`). For each episode:

| Step | Code | Effect |
|---|---|---|
| Choose adapter | `campaign.py:364` | `adapter = W0 if arm == 'adapted' else None`. W0 is `REPOSITORY/harnessadapter/architectures/<ARCHITECTURE>` (`campaign.py:34`). After each learning block it becomes `learned['candidate']` (`campaign.py:398-399`). |
| Episode dirs | `campaign.py:115-118` | `ROOT/runs/<arm>/<NNN>/{workspace,records}`. Raises an error if the directory exists: each episode is attempted once. |
| Prepare workspace | `campaign.py:120` → `prepare_workspace` `campaign.py:77-105` | See 1.2. |
| `task.json` | `campaign.py:121` | Written to `runs/<arm>/NNN/`, outside the workspace. The actor cannot read it: only metadata (`stat`) is allowed on that directory. |
| Broker + actor | `campaign.py:126-128` | One `broker.Broker` per episode, so one port per episode, then `acting.run_actor(workspace, records, prompt_for(task), CORPUS, INDEX, adapter=…, timeout=ACTOR_SECONDS, endpoint=relay.endpoint)`. |

### 1.2 Files placed in the workspace

`prepare_workspace` (`campaign.py:77-105`) calls `acting.prepare_actor_workspace(workspace, adapter,
common_instructions=COMMON)` (`campaign.py:78`, defined at `acting.py:81-133`) and then adds two files itself.

| Path in workspace | Created by | Content | Actor access |
|---|---|---|---|
| `AGENTS.md` | `acting.py:112` (adapter) or `acting.py:116` (none) | See 1.3 | read only (deny-write at `acting.py:215-216,240`) |
| `.codex/config.toml` | `acting.py:117-118` (none: a comment only) or `FW/hosts/codex.py:76-96` (adapter: `# Generated configuration…`, no MCP section for prompt or cards) | Codex ignores it at run time: runtime config is passed on the command line | read only |
| `.codex/agents/` | `FW/hosts/codex.py:66-67` (adapter arms only; empty) | — | read only |
| `.ha-demo-generated.json` | `FW/hosts/codex.py:110-112` (adapter arms) | Deployment info: adapter path, `server: null`, `roles: {}` | read only |
| `.acting-prepared.json` (MARKER) | `acting.py:124-132` | Workspace path, adapter path, sha256 of the common instructions and of `AGENTS.md`, `agents_md_bytes`, `project_doc_max_bytes`, deployment | read only |
| `.actor-home/`, `.actor-tmp/` | `acting.py:119-120` | Empty. These become the shell's HOME and TMPDIR (`acting.py:171`) | read/write |
| `.codex/mwm-runtime.py` | `acting.py:122-123` | MWM architecture only; not present in v8 none, prompt or cards | — |
| `officeqa.py` | `campaign.py:79` | Byte copy of `PLUMB/officeqa_tools.py` (7,240 B; `cmp` identical). `OfficeQACorpus.search` is FTS5 BM25 with OR of up to 16 tokens (`officeqa_tools.py:72-90`). `read` returns at most 150 lines and 24,000 chars (`:92-105`). `list_files` is at `:107-112`. The CLI has only `build`, `search` and `read` (`:115-135`) | read only |
| `README.md` | `campaign.py:80-104` | Absolute corpus and index paths, a Python usage snippet, and "Only this task's workspace accepts writes…" | read only |
| `PROMPT.md` | Written by `FW/hosts/codex.py:99-109` / `FW/hosts/inputs.py:45-49`, then **deleted** at `acting.py:113` | — | — |
| `.client-home/` | `acting.py:486` (at launch, not at prepare) | Codex's own HOME and CODEX_HOME: `state_5.sqlite`, `logs_2.sqlite`, `skills/.system/…` | **read/write** (inside the workspace subpath; not protected) |
| `final.md` | Codex, via `--output-last-message` (`acting.py:325`) | Last agent message | read/write |

Guards: the workspace must be canonical (no symlinks) and empty (`acting.py:88-95`), and must be separate from the
adapter (`acting.py:91-93`), the corpus/index (`acting.py:259-263`) and the records (`acting.py:456-460`).
`build_settings` also re-checks that the adapter argument matches the marker and that `AGENTS.md` still has its
recorded sha (`acting.py:264-269`).

### 1.3 How `AGENTS.md` is compiled per arm

- **none** (`adapter=None`). `AGENTS.md` is exactly `COMMON` from `campaign.py:38-47`, 574 B in every episode
  (`acting.py:116`). The default `COMMON_INSTRUCTIONS` at `acting.py:31-38` is always overridden by `campaign.py:78`.
- **prompt and cards**. `acting.py:103-106` calls `harnessadapter.hosts.codex.prepare(adapter, workspace,
  sys.executable)` (`FW/hosts/codex.py:28-113`):
  1. `render_exports` runs `python -m harnessadapter.adapter.render_context --adapter <adapter>` as a subprocess of the
     controller interpreter (`FW/hosts/inputs.py:8-11`). That calls `Adapter.context()` (`FW/adapter/loader.py:154-161`).
     - **prompt**: `adapter.json` has `"context": "ADAPTER.md"`, so `ADAPTER.md` is served verbatim (`loader.py:156-157`).
       There is no size cap other than `project_doc_max_bytes = 4,194,304` (`experiment.py:32`). Measured sizes:
       1,993–231,899 B, median 115,451 B.
     - **cards**: `"context": {"entrypoint": "interface.py:context"}` → `architectures/cards/interface.py:9-11` →
       `cards_core/render.py:38-57`. That is `PREAMBLE.md` plus the admitted cards in a fixed order, cut to whole
       cards at `CARDS_MAX = 16384` B (`render.py:15, 43-46`). Measured sizes: 1,603–17,282 B, median 9,796 B.
  2. `append_context` (`FW/hosts/context.py:31-42`) wraps that text in `<!-- harnessadapter:<id>:start/end -->`
     markers with a `## Adapter: \`<id>\`` header, and demotes the adapter's headings by two levels (`context.py:13-28`).
     It appends the block after `PROJECT_CONTEXT` (`context.py:6-10`).
  3. Back in acting: `acting.py:109-112` checks that the file starts with `PROJECT_CONTEXT` and replaces that prefix
     with `COMMON`. So adapter arms get `COMMON` + marked adapter block.
  4. `MWM` only: `acting.py:98-102,108` swap in `full_runtime.render_exports` (`full_runtime.py:99-103`). This is not
     used in v8 none, prompt or cards.
- **How it reaches the model** (`api/001/request.json`). `instructions` is Codex's base prompt, about 17 KB. `input`
  holds, in order:
  1. a developer message `<skills_instructions>` listing Codex's bundled system skills from
     `.client-home/.codex/skills/.system` (imagegen, openai-docs, …; about 2.6 KB of noise);
  2. a developer message `<permissions instructions>` saying "No filesystem sandboxing - all commands are permitted.
     Network access is enabled." (false);
  3. a user message `# AGENTS.md instructions for <cwd>` containing the file;
  4. a user message `<environment_context>`;
  5. the task prompt from `campaign.py:108-111`.

---

## 2. How Codex is launched, its config, and the sandbox

### 2.1 Process tree and environments

`run_actor` (`acting.py:454-522`) runs
`subprocess.Popen(['/usr/bin/sandbox-exec', '-p', <profile>, <codex argv…>, '-'], cwd=workspace, env=client_env,
stdin=PIPE, start_new_session=True)` (`acting.py:490-491`). The prompt goes in on stdin (`'-'` at `acting.py:330`,
`communicate(prompt, …)` at `acting.py:493`).

- **Codex's own environment** (`client_environment`, `acting.py:177-181`): `HOME=TMPDIR=<workspace>/.client-home`,
  `PATH=/usr/bin:/bin:/usr/sbin:/sbin`. Because TMPDIR equals CODEX_HOME's parent, Codex prints `Refusing to create
  helper binaries under temporary dir` in all 399 `stderr.log` files, so there is no `apply_patch` alias on PATH. Two
  runs hit `unsupported call: apply_patch`.
- **The actor shell's environment** comes from `shell_environment_policy.inherit="none"` plus
  `.set=tool_environment(ws)` (`acting.py:287-288`, defined at `acting.py:166-174`):
  - `HOME=<ws>/.actor-home`, `TMPDIR=<ws>/.actor-tmp`
  - `PATH=<dir of sys.executable>:/Applications/ChatGPT.app/Contents/Resources/cua_node/bin:/usr/bin:/bin:/usr/sbin:/sbin`.
    `sys.executable` is the controller's interpreter, `adapter-comparison-work/.venv/bin/python`.
  - `LANG`, `PYTHONNOUSERSITE=1`, `PYTHONDONTWRITEBYTECODE=1`, `MPLCONFIGDIR`
  - No `TMPPREFIX` and no `ZDOTDIR`.
- **One sandbox for everything.** The whole Codex client, and every child it spawns, inherits the one Seatbelt
  profile. macOS does not allow a nested `sandbox_apply` (`acting.py:194`), so Codex runs with
  `sandbox_mode="danger-full-access"` (`acting.py:271-273`). That is also why Codex's permission message claims there
  is no sandbox.

### 2.2 Codex argv and `-c` settings

`command_for` (`acting.py:321-330`) produces:
`codex --strict-config --ask-for-approval never exec --ignore-user-config --ignore-rules --skip-git-repo-check
--ephemeral --json --cd <ws> --output-last-message <ws>/final.md -c k=v … -`.
The binary is `frameworks-v8/codex-0.155.0-alpha.9.2/codex` (from `.task-paths.json` via `paths.py:15-18`), and its
sha is frozen (`campaign.py:298, 315-316`).

`build_settings` (`acting.py:256-318`). The values below are as recorded in `invocation.json`; v8 used FULL profiles
for all three roots.

| Key | Value | Line |
|---|---|---|
| model / provider / effort | `accounts/fireworks/models/glm-5p3-flash` / `officeqa_fireworks` / `high` | `26-28, 270` |
| `sandbox_mode` | `danger-full-access` (the outer Seatbelt profile is the real boundary) | `273` |
| `model_providers.officeqa_fireworks` | `base_url=http://127.0.0.1:<port>/v1`, `wire_api=responses`, `request_max_retries=100`, `stream_max_retries=100`, `stream_idle_timeout_ms=3600000` | `274-280` |
| `project_doc_max_bytes` | 4,194,304 | `281`, `experiment.py:32` |
| `model_context_window` / `model_auto_compact_token_limit` | 196,608 / 160,000, overridden under FULL to **1,048,576 / 800,000** | `284, 293-295` |
| `approval_policy` / `web_search` / `project_root_markers` | `never` / `disabled` / `[]` | `285-286` |
| `features.skip_host_skill_discovery` | true (Codex warns that it is under development, in all 399 runs) | `286` |
| `shell_environment_policy.*` | see 2.1 | `287-288` |
| `projects.<ws>.trust_level` | `trusted` | `291` |
| `features.<32 names>` | false (`DISABLED_FEATURES`, `acting.py:39-48`) | `292` |
| `agents.enabled`, `features.multi_agent` | **true under FULL, in all arms including none** | `293-295` |

Tools the model sees (request `tools`): `exec_command` (`cmd, workdir, shell, login` ["Defaults to true"],
`max_output_tokens`, `yield_time_ms`, `tty`, …), `write_stdin`, `request_user_input`, `view_image` and `multi_agent_v1`.
In 399 runs the completed items were only `command_execution`, `reasoning`, `agent_message` and `error`, so the
multi-agent tool was never used.

### 2.3 The Seatbelt profile (`client_policy`, `acting.py:193-248`)

| Permission | Paths | Line |
|---|---|---|
| default | **deny** | `218` |
| process-exec, process-fork | unrestricted (but a binary's file must be readable) | `219` |
| file-read*, file-map-executable | `/System /usr/lib /usr/share /usr/libexec /bin /sbin /usr/bin /usr/sbin /Library/Apple /Library/Preferences /private/etc /private/var/db` + `runtime_roots()` + corpus dir + `corpus.sqlite` + the adapter only if it has an MCP server or roles (never in v8 none, prompt or cards: `acting.py:470-471`) | `203-209, 235` |
| `runtime_roots()` | `sys.prefix` = `adapter-comparison-work/.venv`; `sys.base_prefix` = `~/.cache/codex-runtimes/codex-primary-runtime/dependencies/python`; the **whole framework `harnessadapter/` package**; `/Applications/ChatGPT.app/Contents/Resources/cua_node`; the Codex binary | `136-144` |
| file-read-metadata (stat only, no listing) | every ancestor of the workspace and of each read root (e.g. `/Users`, `…/runs/baseline/001`), plus `/etc /var /tmp /dev` | `213-214, 237-238` |
| file-read*, file-write*, file-map-executable | the workspace subpath | `239` |
| deny file-write* | `AGENTS.md README.md officeqa.py .codex .acting-prepared.json .ha-demo-generated.json` | `215-216, 240` |
| devices | `/dev/null`, `zero`, `tty`, `fd`, `ptmx`, `ttys*`, `random`/`urandom` | `241-246` |
| network | outbound TCP to `localhost:<broker port>` only | `247` |

Two notes. `permission_profile()` (`acting.py:147-163`) is dead code: nothing calls it. The profile never grants
`/opt/homebrew`, `/usr/local`, `/Library/Developer`, `/var/folders` or `/private/tmp`.

### 2.4 Root causes, as reproduced with the recorded v8 profile

Output of `acting_repro/probe.sh` under `v8.sb`, run as `/bin/zsh -lc`:

```
PATH=/usr/local/bin:/System/Cryptexes/App/usr/bin:/usr/bin:/bin:/usr/sbin:/sbin:/var/run/com.apple.security.cryptexd/…/usr/local/bin:…:/pkg/env/global/bin:/opt/homebrew/bin:<…/.venv/bin>:<…/cua_node/bin>
whence python  -> …/.venv/bin/python        whence python3 -> /usr/bin/python3, then …/.venv/bin/python3
python3 --version -> xcode-select: error: No developer tools were found …
python -c 'import numpy' -> ModuleNotFoundError: No module named 'numpy'
rg --version -> zsh:9: operation not permitted: rg
cat <<'X' … -> zsh:11: can't create temp file for here document: operation not permitted
TMPPREFIX=/tmp/zsh   TMPDIR=<ws>/.actor-tmp
ls .. -> ls: ..: Operation not permitted
```

- **Why PATH is reordered.** `/etc/zprofile` runs ``eval `/usr/libexec/path_helper -s` ``. `path_helper` puts the
  entries of `/etc/paths` (`/usr/local/bin`, `/System/Cryptexes/App/usr/bin`, `/usr/bin`, …) and `/etc/paths.d/*`
  (cryptex dirs, `/pkg/env/global/bin`, `homebrew` → `/opt/homebrew/bin`) first, and appends the caller's entries
  after them. Codex always used a login shell (the `exec_command.login` default is true; 21,507 of 21,507 commands
  were `/bin/zsh -lc`).
- **rg.** `/opt/homebrew/bin/rg` exists (a symlink to `Cellar/ripgrep/15.1.0`, which also links
  `/opt/homebrew/opt/pcre2`), but `/opt/homebrew` is not a read root. Exec returns EPERM, so zsh says `operation not
  permitted: rg`. A genuinely missing command still says `command not found` (checked). `command -v rg` prints
  nothing because `stat` is denied. Events show this in all arms: the line `operation not permitted: rg` appears
  150 times (none), 134 (prompt) and 151 (cards).
- **python3.** `/usr/bin/python3` is Apple's shim for the command-line tools. On the host the tools exist
  (`/Library/Developer/CommandLineTools`, Python 3.9.6), but inside the sandbox that path is unreadable. When I granted
  it in a test profile, the shim then failed writing its xcrun cache into `/var/folders/…` (unwritable). Even if it
  worked, it would be Python 3.9 without numpy. In the none arm, 46 of 60 `python3` commands failed (heredoc plus
  Xcode).
- **python and numpy.** `python` exists only in the venv, so it wins the lookup. `.venv/pyvenv.cfg` says
  `home = …/codex-primary-runtime/dependencies/python/bin`, `include-system-site-packages = false`, `version = 3.12.14`.
  The venv's `site-packages` has no numpy, pandas or scipy (it has harnessadapter as an editable install, mcp,
  pydantic, pytest, …). In the none arm, 13 of 14 numpy commands failed. The actors had found the venv python by
  absolute path: that path appears in 481 commands.
- **Heredoc.** zsh 5.9 creates here-document files at `${TMPPREFIX}XXXXXX`. The default is `/tmp/zsh`, and zsh does
  not consult TMPDIR. `/private/tmp` is not writable.
- **Read scope.** The profile is deny-by-default. The only wide grants are the system trees, and the runtime roots
  the actor does not need in the none, prompt and cards arms (venv, framework package, cua_node). The actor cannot
  read its own `runs/<arm>/NNN/` (`task.json`, `records/`) or any sibling episode. I found 205 attempts in 37 runs,
  mostly prompt/035 (53), prompt/047 (47) and none/121 (27). All were denied or empty, and no output contains another
  episode's run path. The FINDINGS line saying 3 runs *listed* sibling directories is not supported by these events:
  those runs attempted and were denied. The attempts cost time, and they were encouraged by Codex's false statement
  that there is no sandbox and network is enabled.
- **Truncation (L0 item "no silent output truncation").** Across the none arm's final requests, 464 of 2,779
  `function_call_output` items were cut to at most 10,226 bytes: a header `Warning: truncated output (original token
  count: N) / Total output lines: N`, then head and tail joined by `…N chars truncated…`. 318 of those calls had asked
  for `max_output_tokens=30000`. The cap does not follow the request. It matches Codex's fallback metadata for an
  unknown model (`Model metadata for … not found. Defaulting to fallback metadata`, 399 of 399 runs).

---

## 3. The smallest v9 changes (L0), with verification status

Principle: put everything the actor needs in a **per-episode, write-protected kit directory inside the workspace**.
The kit holds shims, the L1 CLIs and a zsh profile. Then make the Seatbelt profile *narrower*, not wider. The kit
travels with the episode (its hashes go into the marker), so replay and audit see exactly what the actor had.

### 3.1 Patch sketch (against `PLUMB/acting.py`)

```python
# new constants (paths.py, overridable from .task-paths.json; record both in invocation.json + freeze.json)
ACTOR_PY_PREFIX = Path(_config.get('actor_python_prefix',
                   '/Users/albertzhao/.cache/codex-runtimes/codex-primary-runtime/dependencies/python'))
ACTOR_PYTHON = ACTOR_PY_PREFIX / 'bin/python3'          # -> python3.12; numpy 2.3.5, pandas 2.2.3, FTS5 ok
ACTOR_RG = Path(_config.get('actor_rg', '/Applications/ChatGPT.app/Contents/Resources/rg'))
#   ^ ripgrep 15.2.0, links only /usr/lib/libiconv + libSystem (Homebrew rg also needs /opt/homebrew/opt/pcre2)
KIT = '.officeqa'   # <ws>/.officeqa/{bin,lib,zdot}; protected like AGENTS.md

def actor_path(ws):
    return ':'.join([str(ws / KIT / 'bin'), '/usr/bin', '/bin', '/usr/sbin', '/sbin'])

# prepare_actor_workspace, after acting.py:120
kit = workspace / KIT
(kit / 'bin').mkdir(parents=True); (kit / 'zdot').mkdir()
for name in ('python', 'python3'):
    (kit / 'bin' / name).symlink_to(ACTOR_PYTHON)
(kit / 'bin/rg').symlink_to(ACTOR_RG)
shutil.copytree(TOOLS_SOURCE, kit / 'lib')              # L1 CLIs; TOOLS_SOURCE = frozen or adapter-owned (3.3)
for tool in ('find_tables', 'show_table', 'printings', 'calc', 'ledger'):
    w = kit / 'bin' / tool
    w.write_text(f'#!/bin/sh\nexec "{ACTOR_PYTHON}" -I "{kit}/lib/cli.py" {tool} "$@"\n'); w.chmod(0o555)
# /etc/zprofile runs path_helper first; $ZDOTDIR/.zprofile runs after it and restores our PATH
(kit / 'zdot/.zprofile').write_text(f'export PATH="{actor_path(workspace)}"\n')
# optional belt-and-braces if the model ever passes shell=bash with login: .actor-home/.bash_profile (protect it too)
metadata['kit_sha256'] = {…}                              # sha of every kit file, next to agents_sha256

# tool_environment (acting.py:166-174)
return {'HOME': …/.actor-home, 'TMPDIR': …/.actor-tmp,
        'TMPPREFIX': str(workspace / '.actor-tmp/zsh'),   # zsh here-docs (zsh ignores TMPDIR)
        'ZDOTDIR': str(workspace / KIT / 'zdot'),
        'PATH': actor_path(workspace),                    # no controller venv, no cua_node
        'LANG': 'en_US.UTF-8', 'PYTHONNOUSERSITE': '1', 'PYTHONDONTWRITEBYTECODE': '1',
        'MPLCONFIGDIR': …/.actor-tmp/matplotlib,
        'OFFICEQA_CORPUS': str(corpus), 'OFFICEQA_INDEX': str(index),
        'OFFICEQA_BROKER': endpoint}                      # only if L2 ledger/calc post to the broker (3.3)

# protected names (acting.py:153 and :215): add KIT
protected = ['AGENTS.md', 'README.md', 'officeqa.py', '.codex', MARKER, '.ha-demo-generated.json', KIT]

# runtime_roots (acting.py:136-144): narrower
def runtime_roots(server=False):
    roots = [ACTOR_PY_PREFIX, CODEX, ACTOR_RG]           # a file path works as (subpath …), as CODEX already does
    if server:                                           # only an MCP-serving adapter needs these
        import harnessadapter
        roots += [Path(sys.prefix), Path(sys.base_prefix), Path(harnessadapter.__file__).parent]
    return list(dict.fromkeys(str(p.resolve()) for p in roots))
# client_policy: pass server=bool(deployment.get('server')); drop cua_node only after the smoke test (3.2)

# build_settings (acting.py:270-292): additions
'include_permissions_instructions': False,   # removes the false "no sandbox / network enabled" developer message
# candidates; the keys exist in the 0.155.0-alpha.9.2 binary, semantics must be confirmed (3.2):
'allow_login_shell': False,                  # makes path_helper irrelevant; ZDOTDIR already fixes it for -l
'tool_output_token_limit': <N>,              # or model_catalog_json with a glm-5p3-flash entry (truncation_policy,
                                             # context_window, auto_compact_token_limit); see compaction.md
```

`README.md` (`campaign.py:80-104`) should gain one line: "Paths outside the corpus, the index and this workspace
are denied by the OS (`Operation not permitted`); do not search the disk." It should also give a one-line usage for
each kit command.

### 3.2 What is verified and what still needs checking

Verified locally with `sandbox-exec`, a scratch workspace, and `mk9.py` + `probe9.sh`. The v8 profile had the venv,
`harnessadapter/` and `cua_node` removed, and the kit dir and `ACTOR_RG` added. Both `zsh -lc` and `zsh -c` were
tested.

| Check | v8 | v9 candidate |
|---|---|---|
| PATH after a login shell | reordered by path_helper | exactly `<kit>/bin:/usr/bin:/bin:/usr/sbin:/sbin` |
| `python3 -c 'import numpy, pandas, sqlite3'` | Xcode stub error | 3.12.14, numpy 2.3.5, pandas 2.2.3, sqlite 3.53.1 (FTS5 ok), prefix = codex runtime |
| `rg -c -i treasury <corpus file>` | `operation not permitted: rg` | 58; `rg -l -i 'public debt' <corpus>` gives 697 files |
| `python3 - <<'PY'` | can't create temp file | works |
| venv / `/opt/homebrew` readable | venv yes | both `Operation not permitted` |

Caveat seen in the reproduction: when rg's stdout is a *regular file outside the readable roots*, it prints nothing
and exits 0. This comes from ripgrep's guard against searching its own output file, which needs to `stat` stdout. My
test harness's stdout was such a file. Codex gives commands a pipe or PTY, and through a pipe or into a workspace file
the output was correct.

Not verified, because it needs a live Codex:
- Codex starting up inside the narrowed profile, especially without `cua_node`.
- The semantics of `include_permissions_instructions`, `allow_login_shell` and `tool_output_token_limit` /
  `model_catalog_json`. All four names are present in the binary's config-key strings.

Order of checks:
1. `acting.validate_config_offline(settings)` (`acting.py:525-563`): `app-server --strict-config` plus `config/read`,
   with zero model calls.
2. `run.py smoke` (`run.py:15-65`): one synthetic fixture and one tiny call per arm. Extend it to run the
   `probe9.sh` commands through the real `exec_command` and assert on them. `validate_config_offline` starts the
   Codex binary, so decide whether that counts as "no network" before running it.

### 3.3 Where the L1–L4 pieces plug in

- **Tool code ownership.** If the evolver edits tools or checkers, they belong to the adapter revision, like cards,
  and are copied into the kit per episode. Only the L0 pieces go into `freeze()` / `verify_freeze()`
  (`campaign.py:292-323`): python prefix + version + numpy version, rg sha, shim layout. Today the actor gets no read
  access to the adapter (`acting.py:470-471`), so copying into the protected kit is simpler than widening
  `readable_adapter`.
- **Ledger and calc records.** The workspace is actor-writable, so a ledger kept there can be forged by a plain
  `echo`. The sandbox already allows TCP to the per-episode broker (`acting.py:247`), and the broker already serves a
  side route, `/v1/local-mwm` (`broker.py:228-242`; other paths 404 at `:243-245`). A `/v1/ledger` route would let
  `calc` and `ledger` post entries that the broker stores in `api/` (outside the sandbox) and re-injects after
  compaction.
- **`MPLCONFIGDIR`, `PYTHONNOUSERSITE` and `-I`.** The kit wrappers run `python3 -I`, which ignores `PYTHON*` variables
  and the user site-packages and does not add the script's directory to `sys.path`, so `cli.py` must insert its own
  directory.

---

## 4. How the final answer is captured, timeouts, and what happens at timeout

**Capture.**
1. Codex writes the last agent message to `<ws>/final.md` (`--output-last-message`, `acting.py:325`) and streams JSONL
   events to stdout, which is saved as `records/events.jsonl` (`acting.py:488-494`).
2. `parse_events` (`acting.py:333-375`) sets `turn_completed` only if the last terminal event is `turn.completed`,
   with no `error` event after it and no malformed lines (`:346-348`). It takes `last_agent_message` from the last
   `item.completed` of type `agent_message` (`:361-367`) and also returns `thread_ids` (`:374`).
3. `collect_final` (`acting.py:378-416`) opens `final.md` with `O_NOFOLLOW|O_NONBLOCK`. The file must be regular,
   at most 1 MiB, UTF-8 and non-empty, and it must equal the event's last agent message after stripping; the actor
   could write `final.md` itself, which is why the equality check exists. It then copies the file to
   `records/final.md`.
4. Status (`acting.py:508-510`) is one of `launch_error`, `timeout`, `invalid_final`, `completed` or `failed`.
5. `campaign.actor` (`campaign.py:133-136`) sets `answer = records/final.md.strip()` **only if `status ==
   'completed'`**, otherwise `''`. It then writes `prediction.json`. `answer_format_valid` (at most 250 chars, one
   line) is recorded but never enforced (`campaign.py:163`). The format rule reaches the model only as text:
   `campaign.py:45-46` and `campaign.py:108-111`.

**Timeouts.**
- `ACTOR_SECONDS = 3600 if FULL else 1800` (`experiment.py:22`) is the wall clock for the whole Codex process,
  including provider latency and broker scheduling. Under FULL, the broker's `request_timeout` and `max_completion`
  are `None` (`experiment.py:23-24`), and the rate scheduler is unbounded (`campaign.py:335`).
- Inside Codex, `stream_idle_timeout_ms=3,600,000` and the request and stream retries are clamped to 100
  (`acting.py:278-280`). `exec_command` yields after `yield_time_ms` (250–30,000 ms) and the model polls with
  `write_stdin`.

**What happens at timeout.**
- `communicate(timeout=3600)` raises. `run_actor` then calls `_stop_group` (`acting.py:419-451`): SIGTERM to the
  process group, 5 s grace, SIGKILL. It collects the remaining stdout for 10 s (`acting.py:495-499`). The `finally`
  block kills the group again even after a normal exit, which removes orphan background processes
  (`acting.py:500-501`).
- There is no terminal event, and `final.md` was never written. All 13 timeouts show `final_open_rejected` (ENOENT),
  status `timeout` and answer `''`. `campaign.py:139-144` also clears the transport-failure flags, so an in-flight
  stream is not counted as a transport loss.
- The timed-out runs issued 143–668 commands each (spinning). Intermediate `agent_message` items exist: 2–62 per run,
  for example 50 in prompt/047. But nothing converts them, or any candidate, into an answer.
- Consequence for L3: "submit the best candidate at timeout" has to happen in the controller after `timed_out`. It
  should read the broker-owned ledger (3.3). A soft deadline, for example a broker nudge at 50% and 80% of the clock,
  is needed before the hard kill.

**Consequences for L4 (the commit gate).**
- With `codex exec --ephemeral`, the process exits as soon as the model returns a response with no tool call. No
  session is saved, so `codex exec resume` is impossible.
- Two ways to feed checker failures back, at most 2 rounds each:
  - **(a) Resume (cleaner).** Drop `--ephemeral`; sessions then live in `<ws>/.client-home/.codex/sessions`. After a
    completed turn, run the checkers on `final.md`. On failure, launch `codex exec resume <thread_id> -` with the same
    sandbox, the `-c` settings, `--json` and `-o final.md`, and a controller prompt listing the failed checks.
    `resume` supports `--json`, `-o/--output-last-message`, `--strict-config`, `--ignore-user-config` and
    `--ephemeral`, checked in the binary's `--help`. The thread id is already parsed (`acting.py:374`). The remaining
    wall-clock budget must be carried over, and `invocation.json`'s `feedback_source` (`acting.py:480`) must change.
  - **(b) Broker rewrite.** The broker intercepts the final no-tool-call response and substitutes something else, for
    example a synthetic `exec_command` call to a `commit_gate` CLI. That fabricates model output, so I do not
    recommend it.
- **`--output-schema <FILE>`** also exists on `exec` and `resume`. It could force a structured final message (answer
  plus cited ledger ids), but it changes what `parse_events` and `collect_final` compare.

---

## 5. Places where the arm changes behaviour

| Where | What differs |
|---|---|
| `.task-paths.json` → `experiment.py:6-19` | Profile and arms. The none root uses **`full_cards_unbounded` with `arms: ['baseline']`**, so `FULL=True`, `ARCHITECTURE='cards'` and `MWM=False`. The prompt root uses `full_prompt_only_unbounded`, the cards root `full_cards_unbounded` (both `arms: ['adapted']`). The none arm's Codex settings come from a FULL profile only because of this choice. |
| `experiment.py:22` | `ACTOR_SECONDS` is 3600 for every FULL profile. |
| `campaign.py:34, 364` | `adapter = W0` only for `adapted`; the prompt root's framework is `prompt-c0c9b87-v6f`, the others `next-a48c0f9`. |
| `acting.py:97-118` | none gets `COMMON` + a placeholder `.codex/config.toml`; adapted arms go through `hosts.codex.prepare` and get `COMMON` + the marked adapter block, plus `.ha-demo-generated.json` and an empty `.codex/agents/`. |
| `loader.py:156-157` vs `cards/interface.py:9-11` + `render.py:15,38-53` | prompt: `ADAPTER.md` verbatim, uncapped (up to 231,899 B). cards: preamble + whole admitted cards, capped at 16,384 B. |
| `acting.py:98-102, 108, 122-123, 297-313`; `campaign.py:123-125`; `broker.py:228-242` | MWM only (not used in v8's three roots): render swap, `mwm-runtime.py`, MCP server config, Docker sandbox, `/v1/local-mwm`. |
| `acting.py:293-295` | FULL only, but then for **all** arms: `agents.enabled`, `features.multi_agent`, 1M context window and 800k auto-compact limit. |
| `acting.py:470-471` | The adapter becomes readable only with a server or roles, so never in v8. |
| `campaign.py:336-339, 390-405, 397` | The learner module (`cards_learning` vs `full_learning`) runs only after `adapted` blocks; cards also receives a `stop` event. Nothing in the actor launch differs. |
| Not arm-dependent | Codex argv and `-c` keys, the Seatbelt profile, the tool env, `README.md` (apart from the root path), `officeqa.py`, the prompt text, the timeout, final-answer capture. Checked: argv identical after normalisation, for Q050 in all three arms. |

For a fair v9 run, decouple the Codex settings (context window, multi-agent, L0 kit) from `ARCHITECTURE`. Then a
"none" root need not borrow the `full_cards_unbounded` profile, and L0 is guaranteed identical across none,
prompt-evolve, mech-static and mech-evolve.

---

## 6. Other risks and loose ends

- The codex runtime python is app-managed (`~/.cache/codex-runtimes/…`, built Sep 10) and could change under a
  ChatGPT app update. Pin it in `freeze()` (sha of `bin/python3.12` + numpy version) and refuse to run on a mismatch.
  If it must be immune to updates, copy the prefix into the campaign root once and point `ACTOR_PY_PREFIX` there.
- The ChatGPT.app `rg` can change with app updates too. Copy it into the kit or campaign root rather than symlinking,
  and record its sha (currently `05fe4a082be8f7d5…`).
- `.client-home` (Codex's state and logs) sits inside the actor-writable workspace (`acting.py:486`). It is harmless
  today, but it shows up in `find .`. If `--ephemeral` is dropped for L4 resume, the session files land there too.
  Consider `-not -path './.client-home/*'` hints in the README, or moving it to a sibling directory with its own
  write grant.
- The client's `TMPDIR` equals its home (`acting.py:179`), which causes the apply_patch-alias warning in every run. A
  separate `.client-tmp` would fix it. Low value.
- The bundled system-skills developer message (about 2.6 KB) is noise for this task. There is probably a config key
  to drop it (`include_skills_usage_instructions` / `bundled_skills` strings exist); unverified.
- 21 `exec_command … Failed to create unified exec process: No such file or directory` errors (17 in the none arm)
  came from the model inventing a `workdir`. For example, none/001 dropped `officeqa-pro133-none-v8-work/` from the
  path. A kit CLI that never needs a `workdir` avoids this.

## Glossary

- **Seatbelt / `sandbox-exec`.** macOS's kernel sandbox. A profile is a list of allow and deny rules; `deny default`
  then `allow …` means that only the listed paths and operations work. A denied existing file returns EPERM, shown as
  "Operation not permitted".
- **Login shell (`zsh -l`).** A shell that runs the login startup files (`/etc/zprofile`, then
  `$ZDOTDIR/.zprofile`). On macOS, `/etc/zprofile` runs **`path_helper`**, which rebuilds PATH with the system paths
  first.
- **`ZDOTDIR`.** The directory where zsh looks for the user's startup files. Pointing it at the write-protected kit
  lets the harness run one line after `path_helper` without touching HOME.
- **Heredoc (`<<'PY' … PY`).** Inline text passed to a command's stdin. zsh stores it in a temp file whose name starts
  with **`TMPPREFIX`** (default `/tmp/zsh`).
- **venv.** A Python virtual environment: a folder with its own `site-packages` (installed libraries) layered on a
  base interpreter. `include-system-site-packages = false` hides the base interpreter's libraries, which is why
  numpy was invisible.
- **Shim.** A tiny stand-in, here a symlink or two-line script, placed early on PATH so that a command name
  (`python3`, `rg`, `calc`) runs the program the harness chose.
