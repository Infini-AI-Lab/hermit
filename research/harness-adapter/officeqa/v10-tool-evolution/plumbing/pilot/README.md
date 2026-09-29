# v10 round 1: how to run it (DESIGN §7; PROTOCOL.md §7)

The round runs the 50-question development subset (`first50.json`: the first 50 of the canonical order, sha256
`c95f9613…a07d`) in five roots, one arm each, at temperature 0 without top_p:

- `v10-r1-a`: `plain` (base harness only);
- `v10-r1-b`: `prompt-evolve` (prompt W0 + scoped-note learner);
- `v10-r1-c`: `harness-static` (harness W0: SEED, tools, controls; gate off);
- `v10-r1-d`: `harness-evolve` (harness W0 + harness learner: notes + tool revisions);
- `v10-r1-e`: `harness-evolve-notes` (harness W0 + scoped-note learner; whether it runs is a launch-time budget
  decision — drop it with `--arms plain prompt-evolve harness-static harness-evolve`).

Steps 0–4 are $0. Steps 5–6 are **paid** and need your explicit approval and your Fireworks key. The key is
typed at a hidden prompt or read from `FIREWORKS_API_KEY`; it is never written anywhere.

```
PY=/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/adapter-comparison-work/.venv/bin/python
P=/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/officeqa-pro133-v10-plumbing
cd $P
```

## Files here

| file | what it is |
|---|---|
| `first50.json` | the development subset: 50 ids, their base-order ordinals, the rule, the list sha256 `c95f9613…a07d` (`run.py roots` declares it in every root) |
| `select_first50.py` | the v9 label-free rule that produced it (reads task ids and ordinals only) |
| `select_loop_prone.py`, `loop_prone_20.json` | the v9 label-free loop-prone scan `select_first50.py` cross-checks against (task ids and command repeat counts only). The v9 pilot record (addenda, metrics, rule selection) lives in the v9 tree only; the v9 pilot roots stay untouched in `../v9-p1-*` and `../v9-p2-a` |

## 0. Tests ($0, ~6 min)

```
V9_SLOW=1 V9_LIVE_CODEX=1 $PY -B -m pytest -q -p no:cacheprovider tests
```

`tests/test_v10_e2e.py` runs the whole pipeline on the toy pack with the pinned Codex against a fake upstream
(five arms, gate-off loops and the hard block, a tool revision admitted and served, crash + resume, faults).

## 1. Optional: replay the v9 evidence against the W0 tools ($0, ~2 min)

```
$PY -B preflight.py --replay-v9a ../v9-p1-a       # >= 200 rebuilt invocations, 0 changed outputs
```

Reads only the recorded command lines of `../v9-p1-a` (read-only), runs each rebuilt invocation twice through
the sandboxed ToolRunner over that root's corpus and writes `preflight/replay-v9a.json` (counts, no output).

## 2. Create the roots ($0, no model call)

```
$PY -B run.py roots                                # --parent DIR, --prefix v10-r1, --no-subset, --arms ...
```

This creates `../v10-r1-a` … `../v10-r1-e`, each a copy of this plumbing with its `.task-paths.json`
(profile v10, arm, repeat 1, `order_seed` `v10-officeqa-pro133-order`, temperature 0, the subset, the barrier
`../v10-r1-barrier` over the five roots with a 12 h timeout, `framework`, `data_cache` and `tables_index` =
`../frameworks-v10/officeqa-tables-index/tables.sqlite`, the `otables-index-v10` build with the narrative section
tables; `--tables-index FILE` overrides it). It refuses to run inside a root, when a root or the barrier directory
exists, when the framework / data-cache directories are missing, or when the tables index is missing or not the
tools' version (the frameworks-v9 index is v9.1: the v10 tools open it, but W0-12 would print nothing). Every root copies this tree's `PREREG.md` (the v10 pre-registration, DESIGN §7's `PREREG-v10.md`; written
after S4/S5, first heading `# v10 ...`; the tree carries none until then) once it exists; preflight refuses a root
without it or with a v9-headed one, and `run.py run` refuses a root whose PREREG hash differs from the one preflight
recorded.

## 3. Prepare and check each root ($0)

`data.py prepare` reads the official download controller-side; answers stay under `prepared/private/`, mode 0400.

```
for r in v10-r1-a v10-r1-b v10-r1-c v10-r1-d v10-r1-e; do
  (cd ../$r && $PY -B data.py prepare && $PY -B preflight.py --offline-only) || break
done
```

`preflight.py --offline-only` runs the offline check list and every test tier inside the root (≈ 6 min per
root): spec and profile, data and tables index, frozen runtime, the barrier order, both W0s (drift, the seed
parity checklist and byte counts, `check_adapter`), the W0 idempotence through the learner screens (a TODO until
`preflight.W0_SCREENS` is registered), the ToolRunner smoke (every W0 tool once through the sandbox over this
root's corpus), the first question's workspace (kit, submit iff gate, neutral code root), the declared
temperature / top_p against what the broker sends, and PREREG. `preflight/offline-checks.json` lists every check.

## 4. Readiness smoke (paid, cents per root)

```
for r in v10-r1-a v10-r1-b v10-r1-c v10-r1-d v10-r1-e; do (cd ../$r && $PY -B preflight.py) || break; done
```

Each root runs one synthetic marker question through its own arm (tools from the adapter's code root on the
harness arms; no submit route: the gate is off). It writes `preflight/readiness.json`, which `run.py run` requires.

## 5. The round (paid; start the five together, the barrier pairs their blocks)

```
for r in v10-r1-a v10-r1-b v10-r1-c v10-r1-d v10-r1-e; do
  (cd ../$r && nohup $PY -B run.py run > campaign-stdout.log 2>&1 &)
done
```

`run.py run` asks for the key at a hidden prompt when `FIREWORKS_API_KEY` is not set; export it in that shell
when starting in the background.

- **Cost.** Read it after each block: `(cd ../$r && $PY -B run.py cost | head -c 800)`; add up
  `known_cost_lower_bound_usd`. `replay_seconds` is the $0 compute of the tool replays. To stop, send SIGINT to
  each root (`process.json` holds the pid); a stop writes the group ABORT; `run.py resume --clear-abort`
  continues at the block level.
- **Progress.** Open `../v10-r1-*/report.html`: per-block prequential table, controller line (loop notes, hard
  blocks, observation coverage), learning rounds and admitted tool revisions. No questions or answers.

## 6. After the round ($0)

```
for r in v10-r1-a v10-r1-b v10-r1-c v10-r1-d v10-r1-e; do (cd ../$r && $PY -B run.py audit && $PY -B run.py cost); done
```

`run.py audit` writes `preflight/final-audit.json` (code roots and shas, submit iff gate, controls fixed at W0,
admitted notes and revisions, leak screens, seals, barrier order, subset / temperature / top_p on every record).
The analysis of the named contrasts (DESIGN §7: `d` vs `e`, `d`/`e` vs `c`, `b` vs `a`; system contrasts `c` vs
`a`, `b` vs `d`) is descriptive: a paired difference within ±4 questions is noise, ≥ 8 more than noise.
