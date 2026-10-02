# Claude Code mod: run an evolution session on demand

A [Claude Code mod](https://code.claude.com/docs/en/plugins/mods/overview) that adds `/evolve` to a
session. The command starts a `harnessadapter evolve` run and reports where the candidate landed.

The Evolver does not run inside the mod. `/evolve` starts the framework's own CLI as a detached
process, so a session keeps the execution policy it always had: preflight must pass before a learner
starts, only the candidate and scratch are writable, and the source adapter is never modified. The mod
starts that process and reports what it produced. **It adopts nothing.**

## Why the Evolver stays outside

A mod's hooks run inside Claude Code, with the user's permissions and no sandbox of their own, and a
hook has a ten second budget for one event. An evolution session runs for minutes and depends on an
OS-enforced boundary. Those requirements are incompatible, so the mod only triggers the run.

## Install

Requires Claude Code v2.1.287 or later, where mods are on by default.

```sh
claude --plugin-dir /path/to/examples/claude_code_mod   # one session, reloads on save
```

Set the plugin's configuration before the command will run:

| Setting | Meaning |
|---|---|
| `adapter` | The adapter to evolve: a bundled name such as `mwm`, or an absolute path |
| `learning_material` | Recorded episodes, read-only. Must contain `README.md` |
| `output` | Where sessions are written. Relative paths resolve against the session's working directory. Default `.evolution` |
| `network` | Tool and command network access inside the session. Model API transport is unaffected. Default off |

## Use

```text
> /evolve focus on the sbatch billing flags

● evolve-trigger
  Evolution session started in /work/.evolution. Nothing is adopted automatically;
  its result will appear here.
```

A status line under the prompt reads `evolve: running` while the session works. Claude is not blocked
and the poller never starts a turn of its own. When `control/result.json` appears, the status line
becomes the session's status, a dim transcript line gives the candidate's path, and a toast reports
whether the candidate is ready.

Then read the session's own report before adopting anything:

```sh
cat  .evolution/<session>/control/report.md
git -C .evolution/<session>/adapter log -p
harnessadapter deploy .evolution/<session>/adapter --host claude-code
```

`candidate_ready` means the run completed and structural checks passed. It does not mean the adapter
improved, which is why nothing here deploys on its own.

## Tests

```sh
claude plugin test
```

The tests cover the refusal when the mod is unconfigured, and the result reader against a missing, a
complete, and a half-written `result.json`.
