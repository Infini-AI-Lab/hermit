# Harness-adapter research on OfficeQA Pro

This folder collects every harness-adapter attempt on OfficeQA Pro from 2026-09-20 to 2026-09-29.
The actor throughout is GLM-5.3-flash inside the native Codex CLI, reached through a local Fireworks relay.
The goal is to show that evolving a harness adapter beats two alternatives: a plain harness (Codex + model),
and prompt-level evolution.

**Start with [`report.html`](report.html).** It is a detailed, self-contained page: setup and terms, results for every
round, a per-question grid, real trace excerpts, the v9 and v10 designs, lessons and open questions. Open it in a
browser from a local checkout; GitHub does not render HTML in private repositories.

## Layout

| Path | Contents |
|---|---|
| `report.html` | The full write-up with examples |
| `results/` | Answer-free tables: `per_question_strict.csv`, `per_question_one_percent.csv`, `per_question_status.csv`, `campaigns.json`. `export_results.py` rebuilds them from the local experiment roots. |
| `officeqa/v01-v02-codex70-stopped/` | Protocols and stop records of the two ungraded first attempts |
| `officeqa/v03-codex70-mwm/` | 70 questions, MWM vs baseline, self-feedback learner |
| `officeqa/v04-gold-feedback-mwm/` | 50 questions, learner receives official answers after each block |
| `officeqa/v05-full-mwm-unbounded/` | Full MWM, no inference caps |
| `officeqa/v06-full-mwm-and-prompt-only/` | Checked postmortem ladder, both MWM and prompt-only deployments; v6c–e protocols |
| `officeqa/v07-cards/` | Cards adapter verified by paired actor re-runs |
| `officeqa/v08-pro133-three-arms/` | All 133 questions; none / prompt-only / cards plumbing |
| `officeqa/opus-reference/` | How the Opus 5.5 reference solve was sealed and graded |
| `officeqa/divergence-analysis/` | Where GLM left the right path, per question (`FINDINGS.md` first) |
| `officeqa/v09-mechanism-adapter/` | `design/` (DESIGN.md, reviews, build waves, `pilot-analysis/POSTMORTEM.md`), `plumbing/` (git history holds pilot 1, then pilot 2), `pilot-results/` |
| `officeqa/v10-tool-evolution/` | `design/`, `analysis/`, `w0-build/`, `plumbing/` (snapshot 2026-09-29 17:50 UTC, work in progress), `preflight-records/` |

Framework-level pieces on this branch:

- `harnessadapter/architectures/cards/` and `harnessadapter/evolvers/cards/` hold the cards architecture used by v7 and v8.
- `docs/design/2026-09-24-cards-evolver.md` is the cards design.
- `docs/analysis/2026-09-24-mwm-agentic-coding-diagnosis.md` is the MWM diagnosis.
- Commits `c0c9b87` and `a9d0976` hold the v5 full-MWM framework and the v6 postmortem snapshot, which the v8 prompt arm also ran on. They are reachable in history but not merged into the tip, because they predate the current `main`. To inspect one:

  ```bash
  git worktree add ../mha-v6-snapshot a9d0976
  ```

## Headline numbers (strict, official `reward.py`)

| Round | Arms | Result |
|---|---|---|
| v3 (70 q) | baseline / MWM | 23 / 24 |
| v4 (50 q) | baseline / MWM + gold | 22 / 22 |
| v5 | baseline / full MWM | 17 / 20 |
| v6f | baseline / MWM | 26 / 24 |
| v6f | baseline / prompt-only | 19 / 25 |
| v7 | baseline / cards | 22 / 26 |
| v8 (133 q) | none / prompt-only / cards | 62 / 64 / 55 |
| Opus 5.5 reference (133 q) | solver + reviewer | 87 |
| v9 pilot 1 (50 q) | text-static / mech-static / text-static T=1.0 | 31 / 20 / 24 |
| v9 pilot 2 (stopped at 25) | mech + advice text | 13/25 (text-static 15/25 on the same) |
| v10 | five arms | not started (calibration running at snapshot time) |

Identical baselines on the same 50 questions scored between 17 and 26, so single-run differences of a few questions are noise. See the report's "How to read these numbers" note.

## What is not in git, and why

Run records (Codex events, broker request logs), prepared data, learner outputs, cost ledgers and the Opus solve logs all contain values equal to OfficeQA reference answers. The answers are public but gated behind a Hugging Face agreement, so these files stay on the machine that produced them. Also left out:

- the 697-file corpus and its search and table indexes (`officeqa_prepare.py` and `otables.build_index` rebuild them);
- the frozen Codex binary (`0.155.0-alpha.9.2`) and actor Python;
- all API keys.

Every committed file was scanned against the 133 reference answers and for key patterns before commit.

The code reads the Fireworks key from the `FIREWORKS_API_KEY` environment variable. Each root's `.task-paths.json`, which is not committed, points it at the local framework copy, the Codex binary and the tables index.

## Running v10 (from a prepared root)

```bash
python run.py roots --prefix v10-r1                  # $0: create the five neutral-named roots
python preflight.py --offline-only                   # $0: data hashes, adapters, seed parity, screens
python calibrate.py --source-root ../v9-p1-a --actor-runs 20   # paid: one learner session
python preflight.py                                  # paid smoke (cents): writes readiness.json
python run.py run                                    # paid: the campaign
python run.py audit ; python run.py cost ; python run.py report
```

Each version's `PROTOCOL.md` (and, from v9, `PREREG.md`) states exactly what that round ran.
