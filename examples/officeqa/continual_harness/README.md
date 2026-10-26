# OfficeQA with Continual Harness

[Continual Harness](https://arxiv.org/abs/2605.09998) (Karten et al. 2026; [code](https://github.com/sethkarten/continual-harness))
on OfficeQA in the SkillOpt paper's setting, with Codex as the acting agent: the harness is Codex's own tree
(`AGENTS.md` as the base policy, `sub_agents/`, `.agents/skills/` with executable helpers, `memory/NOTES.md`; the
[`continual-harness`](../../../hermit/algorithms/templates/continual_harness/) template), and the
[`continual_harness`](../../../docs/methods/continual_harness.md) Evolver refines its four components in place every
training step, from the step's trajectories and the judge's feedback, with no validation gate.

```bash
python examples/officeqa/continual_harness/make_seed.py                    # AGENTS.md = SkillOpt's initial skill
export OFFICEQA_CODEX_IMAGE=officeqa-codex:0.162.0
hermit check --config examples/officeqa/continual_harness/smoke_codex.yaml
hermit train --config examples/officeqa/continual_harness/smoke_codex.yaml  # 4 questions, 2 steps, then eval 4
hermit train --config examples/officeqa/continual_harness/run_codex.yaml    # 50 training questions, 4 epochs
hermit eval  --config examples/officeqa/continual_harness/run_codex.yaml --adapter final
```

The acting agent, the Codex image, the models (GLM 5.3 Flash acts and refines, medium effort) and the one-pass
evaluation are those of the all-Flash GEPA and SkillOpt runs (`../gepa/run_codex_flash.yaml`,
`../skillopt/run_codex_flash.yaml`) and of the Harness² stream (`../harness2_stream/`), so the four methods compare
on one row of settings. What differs is the method: Continual Harness uses the training questions' judge feedback
(as GEPA and SkillOpt do) but never scores a candidate or selects on validation; Harness² uses no labels at all.

`stream_codex.yaml` is the paper's own protocol instead: no training split; the 172 test questions arrive one per
step in `stream_s0.txt` (the Harness² stream's seed-0 order), each answered by the current harness before the
Refiner learns from that one trajectory, with `feedback: false` (the Refiner sees the trajectory, never the judge's
verdict, as in the paper); the stream's own answers are the result.

```bash
hermit train --config examples/officeqa/continual_harness/stream_codex.yaml   # 172 steps; the result is the stream
```

Each step's `step_NNNN/output/report.md` says what each pass changed; `workspace/state/continual_harness.json` is the
log of every step. A session's notes come back in its trajectory directory as `memory/NOTES.md`.

## Result

One stream (`stream_codex.yaml`, run `ch-stream-s0`, 2026-10-24): the 172 test questions in the Harness² stream's
seed-0 order, each answered once by Codex 0.162.0 with GLM 5.3 Flash (medium) acting and refining, `feedback:
false`, 3,600 s per session; exact match; one pass, so each accuracy carries a standard error of about 3.7 points.

| Stream | accuracy |
|---|---|
| Harness², reference execution of the initial skill (the harness both streams start from) | 0.576 |
| **Continual Harness, the stream's answers** | **0.634** |
| Harness², deliverable | 0.651 |

Paired per question with 95% bootstrap intervals: Continual Harness − initial skill **+5.8** [−1.2, +12.8] (24
questions won, 14 lost); Continual Harness − Harness² deliverable −1.7 [−7.6, +4.1] (11 won, 14 lost). The first
86 questions score 0.663 and the last 86 0.605 (Harness²: 0.640 then 0.663); whether the growing harness costs the
second half is not settled by one stream. Every step but one changed the harness; no pass was rolled back by the
template check. The final harness: `AGENTS.md` at its 8,000-character cap, 40 skills with 37 helper scripts, 20
sub-agents, 62 memory notes (39 KB of the 40 KB budget).

Spending and time: executions USD 2.81, Refiner USD 3.67, 8.6 h (about 0.04 USD and 3 minutes per question;
Harness² on the same stream: USD 27.5 and 20.5 h). The two streams and GEPA's and SkillOpt's trainings are on one row
of settings in the [recipe's Flash row](../../../docs/recipes/officeqa.md#the-flash-row); the paper's own
evaluation is a mean over seeds, and this is one stream.
