# OfficeQA Pro: GLM + Codex, with and without an evolved adapter

## Full profiles: unbounded inference with checked postmortems

Two opt-in profiles remove client-side actor/learner deadlines, cumulative
token/call/cost budgets and per-response token/reasoning limits. Fireworks
provider limits/defaults remain. Nothing is cut: transport drops are retried
and counted, the served `AGENTS.md` size is recorded against a 4 MiB limit, and
a question is empty only when Codex produced no completed turn. Both arms
advance in lockstep, one five-question block at a time.

- `full_mwm_unbounded`: the adapted arm uses the MWM architecture (memory,
  briefing, examples, tested tools, roles, wm MCP tool). A local Linux
  container executes native bubblewrap capability tests/tools; it has no
  network and mounts only the current read-only adapter. The actor never
  receives the Docker socket.
- `full_prompt_only_unbounded`: the adapted arm uses the prompt-only
  architecture. Its single learned document `ADAPTER.md` is served verbatim
  inside the actor's `AGENTS.md`; no MCP server, roles, tools or Docker.

After each sealed batch the controller releases that batch's reference answers
and strict/1% grades to the learner, which runs a ladder postmortem (vintage,
table, units, cells, convention, rounding, harness) using two controller-run
checkers (`verify_cell`, `verify_answer`). The controller replays every lesson's
recorded checks and writes its status; MWM serves verified lessons only, while
prompt-only serves `ADAPTER.md` whole with each lesson's status line. The
learning round after Q50 is skipped. See [the full
protocol](PROTOCOL-FULL-MWM.md); the historical results below are not results
of these profiles.

```sh
docker build -t officeqa-mwm-exec:20260921 tasks/OfficeQA/execution   # MWM only
python tasks/OfficeQA/launch.py check --native
python tasks/OfficeQA/launch.py prepare --profile full_mwm_unbounded \
  --data-cache /absolute/path/to/authorized-officeqa-cache \
  --run-dir /absolute/path/to/new-private-run \
  --shelf /absolute/path/to/extracted-reference-text   # optional, learner only
python tasks/OfficeQA/launch.py run --gold-feedback \
  --run-dir /absolute/path/to/new-private-run
```

`--shelf` copies every `*.txt`/`*.md` of a directory into `RUN_DIR/reference-shelf`
for the learner alone; its hashes are recorded at preparation and frozen with the
campaign. The container image (MWM) is frozen by ID during preparation. Calls
and costs are recorded. No new score is claimed until the campaign completes
and is graded.

This task packages the most recent **50-question paired OfficeQA experiment**.
Both arms use `accounts/fireworks/models/glm-5p3-flash` through native Codex.
Only the adapted arm learns from completed batches, using GLM as its learner.

| Setting | Baseline | Adapted |
| --- | --- | --- |
| Model and acting harness | GLM-5.3-Flash + native Codex | Same |
| Questions | Same ordered 50 | Same ordered 50 |
| Documents | 697 parsed Treasury Bulletin TXT files + FTS5 | Same |
| Per-question budget | 30 min; 65,536 cumulative output tokens | Same |
| Model/tool request count | Uncapped | Uncapped |
| Adapter | None | Generic W0, then latest admitted MWM |
| Learning | None | After every 5 submitted questions |
| Learning feedback | None | GLM compares its own traces with those 5 reference answers |
| Per-learning-session budget | — | 30 min; 65,536 cumulative output tokens |

This is **answer-supervised online memory adaptation**: the learner changes
Markdown memory, not model weights. It is an offline subset, not the official
133-question PDF-and-web leaderboard setting. See [PROTOCOL.md](PROTOCOL.md).

## Latest recorded result

Historical run: **2026-09-21**, framework
`81342794b83f115a358011afcd5453b79aed1bd2`, native Codex `0.155.0-alpha.9`.

| Metric | Baseline | Adapted |
| --- | ---: | ---: |
| Strict correctness, numeric tolerance 0 | 22/50 (44%) | 22/50 (44%) |
| Secondary numeric tolerance 1% | 30/50 (60%) | 29/50 (58%) |
| Valid final answers | 41/50 | 42/50 |

Paired strict outcomes: both correct 17, baseline only 5, adapted only 5,
neither correct 23. This experiment does **not** establish a net improvement.

Learning rounds 1, 5 and 8 were admitted. Rounds 2, 3, 4, 7, 9 and 10 exhausted
the output budget; round 6 exceeded the model's input context. Admission means
the session finished and passed mechanical checks, not that the lessons were
judged useful. No human/Astra semantic approval was applied. The final round
occurs after Q50 and cannot affect these scores.

- [Self-contained HTML report](results/2026-09-21-pro50/report.html)
- [Summary, learning rounds and adapter lineage](results/2026-09-21-pro50/summary.json)
- [All 50 paired outcomes, without answers](results/2026-09-21-pro50/per-task.json)
- [Original artifact and execution-source hashes](results/2026-09-21-pro50/provenance.json)

Known formal API charges were at least **$16.1043** at the recorded public prices;
one rejected request has unknown usage. Including the synthetic model preflight,
the lower bound is **$16.1071**. These are usage-based estimates, not invoices.

These files are a historical export. **The updated framework has not been rerun
on the benchmark.** Full traces, predictions, answer-derived memory and official
data remain private local artifacts; the public hash manifest alone cannot
independently verify their contents.

## Layout and responsibilities

Like [CLBench](../CLBench), task-specific orchestration, deployment materials and
tests live under `tasks/`, outside the generic framework:

```text
OfficeQA/
  launch.py                    # explicit download/prepare/check/run/report/audit/export
  host/
    acting.py                  # native Codex, isolated configuration and OS sandbox
    broker.py, client.py        # fixed Fireworks transports, usage and deadline accounting
    campaign.py                # paired arms, immutable submissions, batch answer release
    learning.py                # GLM-only file-tool learner and mechanical admission
    experiment.py              # frozen profile switches (architecture, lockstep, limits)
    full_learning.py, postmortem.py # full-profile learner: graded batch, ladder, checkers, replay
    full_runtime.py, full_sandbox.py # MWM container bridge and sandbox wiring
    data.py, officeqa_prepare.py# deterministic split, private keys, sealed final scoring
    officeqa_tools.py           # local document search/read helper
    audit.py, full_audit.py, cost.py # recorded-artifact checks and dated cost estimates
    export_results.py, report.py# answer-free result tables and standalone HTML
  deployments/officeqa/         # venue-specific learner contract and environment
  deployments/officeqa-full/    # full MWM profile texts
  deployments/officeqa-prompt-only/ # full prompt-only profile texts
  tests/                       # synthetic data, fake model transport, native sandbox tests
  vendor/officeqa/              # pinned official reward.py + Apache license/notice
  results/2026-09-21-pro50/      # safe historical result export
```

**Host** means the Codex program that lets GLM use tools. **Environment** means
the read-only documents and the current question's writable workspace.
**Learner** is another GLM session that reviews the completed batch and edits
adapter memory. **Grader** is the pinned official `reward.py`, run by the
controller after original predictions are sealed. No model judge is used.

The actor never sees the master answers, source mappings, future questions,
the other arm's workspace or past experiment logs. The learner receives only
the current completed five-question batch and previously admitted memory.
Only the controller can release answers, after submission hashes are sealed.
Baseline and adapted actors may run concurrently; the adapted arm waits for its
learner between batches. There is no repair or resubmission of a scored question.

In the historical `memory_bounded` profile the learner has file tools only. It
cannot revisit the whole corpus or run calculations to verify a proposed
correction, and it must read all four evidence files for every completed
question before writing feedback. The full profiles above are that separate,
evidence-verifying treatment: the learner receives the sealed batch's grades
and failure records, may investigate the corpus and shelf, and its lessons are
replayed by the controller. A `memory_bounded` rerun today keeps its budgets but
shares the retrying transport, the no-void rule and the per-episode failure
records introduced with the full profiles; it no longer reproduces the historical
v4 transport behaviour byte for byte.

## Install and check

Run from the repository root. Python 3.11+ is required. The paid actor runner
currently requires **macOS Seatbelt** and a native Codex executable supporting
`--strict-config`, the documented feature flags and `sandbox -P`. The historical
binary was `0.155.0-alpha.9`. Unsupported versions fail the native check.
Do not replace the OS sandbox with instructions to stay in a folder.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m pip install -r tasks/OfficeQA/requirements.txt
.venv/bin/python tasks/OfficeQA/launch.py check --native
```

`check --native` makes **no paid model calls**. It exercises actual native Codex
configuration, OS read/write/network denials, MCP tools, large evidence reads,
and admission using synthetic model responses. `check` without `--native` omits
the optional full native probes but does not authorize a paid run. Some existing
unit tests also exercise local processes and sockets; use a normal local shell.

The launcher prefers the desktop app's native Codex binary when installed.
To select another native binary, set `OFFICEQA_CODEX=/absolute/path/to/codex`
for the checks, or supply the same path to `prepare --codex`. The learner's
sandbox executable is pinned through PATH to the same binary. Use an editable
installation so sandboxed MCP subprocesses can import this checkout.

## Prepare the fixed data

If you already have an authorized download with `download_receipt.json`, use it
directly. Otherwise, obtain access to the [official dataset](https://huggingface.co/datasets/databricks/officeqa),
authenticate the local HF client, and explicitly download:

```sh
.venv/bin/python tasks/OfficeQA/launch.py download \
  --data-cache tasks/OfficeQA/.data/officeqa-official --authorized-auth
```

The pinned dataset revision is `763a8366abf2a3605c381d53586d844dc60fa756`.
The evaluator is pinned to Databricks commit
`7b9a3c154ef9fb40215bb67934afc43e6799de16`; its SHA-256 is checked before use.
The seed is `metaworld-officeqa-pro-codex70-online-v1`: sort by
SHA256(seed + NUL + task ID), then take 50. This preserves the first 50 of the
earlier 70-question order, independently of answers or previous results.

```sh
.venv/bin/python tasks/OfficeQA/launch.py prepare \
  --data-cache tasks/OfficeQA/.data/officeqa-official \
  --run-dir tasks/OfficeQA/.runs/pro50-001
```

Preparation copies the runner, deployment and evaluator into a new private run
directory, verifies all 698 downloaded files, and constructs the corpus index.
It does not call a model. Existing run directories are never overwritten.
`.runs/` and `.data/` are ignored by Git. Keep a custom run directory private too.

## Run and inspect

Provide `FIREWORKS_API_KEY` through your environment, or enter it at the hidden
prompt. Do not put a real key into source code. **The following command is paid**:

```sh
.venv/bin/python tasks/OfficeQA/launch.py run \
  --run-dir tasks/OfficeQA/.runs/pro50-001 --gold-feedback
```

`--gold-feedback` selects the completed-batch supervision protocol; it does not
accept dataset terms or claim publisher permission for a particular use.
The launcher requires passing native checks for the exact implementation, then
runs a paid synthetic model/tool smoke test before dispatching benchmark tasks.
Actor network access is restricted to its own localhost inference relay; only
the controller forwards to the fixed Fireworks API/model. Learner file tools
have no network. No inherited user Codex settings or other models are used.

```sh
.venv/bin/python tasks/OfficeQA/launch.py report --run-dir tasks/OfficeQA/.runs/pro50-001
.venv/bin/python tasks/OfficeQA/launch.py audit --run-dir tasks/OfficeQA/.runs/pro50-001
.venv/bin/python tasks/OfficeQA/launch.py cost --run-dir tasks/OfficeQA/.runs/pro50-001
```

The live report is `RUN_DIR/report.html`. Native trajectories, requests, response
usage, original predictions, batch seals and GLM feedback stay under `RUN_DIR`.
`cost` uses explicitly dated 2026-09-21 rates; refresh those rates before treating
an estimate from a later campaign as current billing.

To publish a completed run's answer-free summary:

```sh
.venv/bin/python tasks/OfficeQA/launch.py export \
  --run-dir tasks/OfficeQA/.runs/pro50-001 \
  --output tasks/OfficeQA/results/my-completed-run \
  --framework-revision FULL_COMMIT_FROM_RUN_FREEZE_JSON
```

## Migration from the local v4 experiment

The original runner depended on sibling `baseline-work` and a fixed local
project layout. This port includes those helpers, uses explicit runtime paths,
and vendors the unchanged official grader with its license and notice.

The updated upstream creates Git repositories inside candidate adapters.
Content snapshots now exclude controller-owned `.git` metadata, while candidate
knowledge edits and their GLM provenance remain checked. Admission compares
learned edits with the **prepared** candidate, so framework-generated deployment
contracts are not misclassified as model edits. Native core protections still
apply. The task supplies explicit OfficeQA deployment slots for the new MWM
interface; no task-specific solution suggestions were added.

New runs freeze the runner, deployment, framework package and generic W0. The
framework's updated prompt/maintenance behavior is not backdated into the stored
v4 score. Changes to these files after preparation require a fresh run directory.
