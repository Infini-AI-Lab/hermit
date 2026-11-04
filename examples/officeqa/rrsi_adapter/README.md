# OfficeQA with RRSI Adapter

[RRSI](https://arxiv.org/abs/2609.24972) (Xia et al. 2026; [code](https://github.com/google-research/rrsi)),
regularized recursive self-improvement, with the harness confined to the adapter, on OfficeQA in the SkillOpt
paper's setting, with Codex as the acting agent. RRSI evolves the harness program itself; here the agent is fixed and
only the adapter changes. The [`rrsi_adapter`](../../../docs/methods/rrsi_adapter.md) Evolver runs upstream's code
for every rule and for the analyst and the critic, with the proposer and the search roles as Codex sessions; each
round's two candidates are evaluated on the 50 training questions with two trials each (the evolve
set), the incumbent after the last round (H_T) is evaluated once on the 172 test questions. The adapter is the
[`rrsi-adapter`](../../../hermit/adapter/templates/rrsi_adapter/) template: as in Meta-Harness Adapter, every file
outside `.hermit/` is the proposer's to write.

```bash
python examples/officeqa/rrsi_adapter/make_seed.py      # ADAPTER.md = SkillOpt's initial skill (H_0)
python examples/officeqa/rrsi_adapter/make_rows.py      # rows/: each training question twice, `<id>#1`, `<id>#2`
cd examples/officeqa/rrsi_adapter
hermit train --config smoke_luna.yaml                  # 4 questions x 2, one round of two: a check of the plumbing
hermit train --config run_luna.yaml                    # 10 rounds of two
hermit eval  --config run_luna.yaml --adapter final    # H_T once on the 172 test questions
hermit eval  --config run_luna.yaml --adapter seed_adapter   # H_0 the same way, in the same window
```

The setting is the README's GPT-5.6-Luna column ([the recipe](../../../docs/recipes/officeqa.md#the-flash-row-with-gpt-56-luna);
the image, the egress proxy and the login: [A model on a ChatGPT login](../../../docs/recipes/officeqa.md#a-model-on-a-chatgpt-login)):
Codex 0.164.0 with GPT-5.6-Luna at medium effort acts in a container per question on the machine's ChatGPT login,
through the framework's Codex agent, which deploys a candidate as `hermit deploy` does (`ADAPTER.md` in `AGENTS.md`,
every other file of the adapter under `hermit/rrsi-adapter/` and named there), 3,600 s per session. The proposer is
a Codex session with the same model on the same login in hermit's sandbox (`evolver.base: codex`), and so are the
analyst, its digesters and the critic, one session per call. No API key is used.

| File | What it is |
|---|---|
| `rrsi.json` | RRSI's hyperparameters: the coding instance's (k 2, m 2, b 1..4, w 3, m_draft 1, n_prune 4, beta0 0.10, w_s 0, w_c 15, w_n 0.5, five repairs, 30 + 6 traces) with beta1 = 25 (25% tokens per additional passing trial of 100) and T = 10 rounds (a budget choice, as Meta-Harness Adapter runs 6 rounds here); delta measured from H_0's evaluation |
| `domain.py` | OfficeQA as RRSI's domain: the critic's patterns (question ids, bulletin file names, the answers and the scorer), plus the training answers the judge's feedback has revealed (each whole answer of four or more characters, and each number in one with three or more significant digits; bare years and round numbers are left to the LLM critic), Python an `exports.yaml` names (it would run outside the agent's container), the components the reserved slot may ask for (the five the reported runs could carry), the smoke tasks (two questions the incumbent answered, one trial each) |
| `texts/` | the constitution (`SKILL.md`), the pattern library (`PATTERNS.md`) and the four roles' domain paragraphs (`briefs.json`), adapted from upstream's coding and workspace domains |
| `trials.py` | the acting agent (the framework's Codex agent) and the judge on a trial row: `<id>#<k>` runs and scores as the question `<id>` |
| `task-info/README.md` | the task and how the agent receives the adapter, for the proposer |

Each step's `step_NNNN/output/report.md` says what the step recorded, decided and handed back; `r<t><v>/` there holds
a variant's proposer sessions. `workspace/state/rrsi/` holds upstream's records (the frontier, delta's calibration,
the edit history, the attribution of each edit's predictions, and per round the analysis, the directives, each
variant's proposal, critic verdicts, diff and smoke, and the decisions); `workspace/state/candidates/<id>/adapter/`
each candidate's files.

## Result

Four trainings with this Evolver (`run_luna.yaml`, runs `luna-rrsi-p1` to `luna-rrsi-p4`) and one with the
engineering instance's selection weights (`run_luna_eng.yaml`, run `luna-rrsi-eng-p`), on 2026-11-02 and 03 at commit
`859bda9`, five side by side on one ChatGPT login; each final adapter H_T was evaluated once on the 172 test
questions, and H_0 once in the same window (run `luna-rrsi-h0-p`). Codex 0.164.0 with GPT-5.6-Luna (medium) acting and
learning, 3,600 s per session; exact match. Besides their names (`trainer.run_name` and `trainer.tree_dir`; the
configs' own are those of the runs before the port), the runs and their evaluations had three settings on the command
line: `agent.concurrency=6`, and `agent.codex.container.proxy=http://oq-egress-chatgpt:3128` with
`sandbox.allow=[chatgpt.com]`, an egress proxy started as `oq-egress-login` is but letting through chatgpt.com only
(`egress_proxy.py 3128 chatgpt.com:443`), so that no session could refresh, and so rotate, the login the five shared.
Data: the 50 training and 172 test questions of SkillOpt `79124b3`'s split (its 24 validation questions unused),
`databricks/officeqa` at revision `763a836` (`officeqa_full.csv` with SHA-256 `b5eb1ce0cb34…`), eight items drifted
from the split ([Reproduction limits](../../../docs/recipes/officeqa.md#reproduction-limits)). A one-pass accuracy
carries a standard error of about 3.7 points.

| Test (172), one pass each | accuracy [95% bootstrap] |
|---|---|
| H_0, SkillOpt's initial skill, this harness | 0.355 [0.285, 0.430] |
| **RRSI Adapter, H_T, mean of four trainings** | **0.616** [0.552, 0.680]: 0.634, 0.640, 0.576, 0.616 (sd 0.029) |
| RRSI Adapter, the engineering instance's selection weights, one training | 0.622 [0.552, 0.698] |

Paired per question with 95% bootstrap intervals, the four trainings averaged per question first as the column
averages GEPA's and SkillOpt's four, against the GPT-5.6-Luna column: RRSI Adapter − H_0 **+26.2** [+20.1, +32.6] (63
won, 13 lost); − GEPA +1.6 [−1.6, +4.8]; − SkillOpt **+7.0** [+3.3, +10.6]; − Harness² +0.6 [−4.2, +5.5]; −
ReHarness **+9.9** [+4.5, +15.6]; − Continual Harness **+14.0** [+8.3, +19.6]; the engineering arm − H_0 **+26.7** [+19.8, +33.7] (49 won, 3 lost). ACE's 0.640 is the column's highest
cell (no paired difference here: its per-question records are not in this repository, and it keeps the playbook with
the best validation score). Almost all of the gain over H_0 is the
answer's form, as in every other cell of the column that was checked (all but ACE's). Every non-exact answer was
read for whether its value is right (each read as right, or where the reading and an automatic match disagreed, read
again independently: the same verdict all 43 times; an answer an earlier check had read, the same answer to the
same question, kept that verdict: 116 of the 284 distinct): the share of answers with the right
value is 0.680 for H_0 and 0.712 over the four trainings (0.692–0.733 each); paired on the value alone, the four
trainings − H_0 is +3.2 [−0.1, +6.7]. What changes is the form: the answers with the right value in a form the judge
rejects fall from 56 to 16.5 per training on average. One test session of the 1,032 gave no answer (`luna-rrsi-p3`,
UID0193: the model's reply hit its output limit, `max_output_tokens`); none timed out.

On the evolve set (the 50 training questions, two trials each):

| Run | delta | H_0 | candidates measured / refused | accepted (inside the noise band) | H_T: S, files | rollouts; tokens of the rollouts + the Evolver; time |
|---|---|---|---|---|---|---|
| `luna-rrsi-p1` | 0.050 | 0.46 | 20 / 0 | 4 (3) | `r09A` 0.66: `ADAPTER.md`, a skill and a tool, 8.8 KB | 2,100; 431 M + 164 M; 6.3 h |
| `luna-rrsi-p2` | 0.061 | 0.41 | 20 / 0 | 1 (0) | `r00B` 0.65: `ADAPTER.md`, 2.1 KB | 2,100; 406 M + 179 M; 6.6 h |
| `luna-rrsi-p3` | 0.056 | 0.38 | 20 / 0 | 3 (2) | `r07A` 0.68: `ADAPTER.md`, 5.5 KB | 2,100; 429 M + 148 M; 5.8 h |
| `luna-rrsi-p4` | 0.053 | 0.37 | 20 / 0 | 4 (2) | `r08B` 0.68: `ADAPTER.md` and a statistics script, 6.3 KB | 2,100; 412 M + 148 M; 5.6 h |
| `luna-rrsi-eng-p` | 0.045 | 0.43 | 20 / 0 | 4 (3) | `r07A` 0.67: `ADAPTER.md`, a skill and a tool, 9.2 KB | 2,200; 508 M + 153 M; 6.7 h |

- **Round 0 makes the gain.** In every training the first round's accepted candidate rose 0.17–0.28 above H_0 on the
  evolve set with prompt edits (in all five, a final gate that puts only the requested value or values in `<answer>`,
  in the requested shape, unit and precision; in four beside a ledger of the operands' sources, in `luna-rrsi-p2`
  beside a reconciliation of the candidate rows run only when two plausible ones remain); every later acceptance is
  inside the noise band but one, `luna-rrsi-p4`'s `r06A` (0.72, +0.09 over an incumbent admitted inside the band at
  0.63). The calibrated band is narrow: H_0's own evaluations across the runs (0.46, 0.41, 0.38, 0.37, 0.43, and
  0.38 when the engineering arm evaluated it again) have a standard deviation of 0.035, which puts the band for a
  difference at about 0.10, against a calibrated delta of 0.045–0.061; inside that band `r06A`'s +0.09 is noise too.
- **The domain's texts name the lever.** OfficeQA's pattern library (`texts/PATTERNS.md`) opens with answer-form
  mechanisms ("a rule that the content of `<answer>` is the value alone in the form the question asks for"), the
  analyst's brief calls the answer's form as important as its value and near misses in form high-leverage, and the
  critic's brief cites "give the value alone in the unit and precision the question names" as acceptable craft;
  upstream's coding and workspace libraries, the ones these adapt, have no such entry (its engineering domain's opens
  with the analogous "Submission-contract compliance", a design that is right but submitted in the wrong form scores
  zero). These texts were written after the column's
  answer-by-answer check had shown that its gains are the answer's form, so round 0's gate is in part what the texts
  suggest, and the comparison with the column's other methods, whose learners were given the task's scoring rule but
  no such library, is of RRSI Adapter with these texts, not of RRSI's search alone. A training without the entry was
  not run.
- **The exploration slot works.** Every variant of every round reached evaluation (20 of 20 in each training), and
  each training measured every component the reserved slot may ask for (the domain's five carriers: `prompt`,
  `config`, `client_tool`, `skill`, `memory`; the hooks and roles an `exports.yaml` now connects were never asked
  for). Three final adapters carry more than `ADAPTER.md`, each file admitted inside the band: `luna-rrsi-p1`'s a skill
  (`skills/evidence_recovery/SKILL.md`, a bounded procedure for an operand missing from the text at hand), admitted
  for its novelty (`r08A`, 2.7% more tokens), and a tool (`tools/local_recover.py`, a read-only search for files
  matching a few terms), for a token saving (`r09A`, 8.5% fewer); the acting agent opened the skill in 7 and ran the
  tool in 3 of the 172 test sessions; `luna-rrsi-p4`'s `scripts/stat_calc.sh` (geometric mean, standard deviations, trimmed mean),
  for a token saving (`r02B`, 2.8% fewer), run in none; the engineering arm's a tool (`tools/audit_ledger.py`, a check
  of the extracted operands) and a skill (`skills/semantic_check/SKILL.md`, an interpretation card before any
  arithmetic), for their score inside the band (`r01B`, `r02B`), which its weights count, run in 166 and opened in
  167. A session counts as running a script when one of its commands executes it (directly, through an interpreter or
  from a `python -c` payload), not when it only reads it.
- **Selection inside the band.** With the coding instance's weights (w_s = 0) a candidate inside the band is admitted
  for any token saving, or for a new structural component (`client_tool`, `skill`, `memory`) that adds less than about
  3% tokens (w_n / w_c = 0.5 / 15), and one that scores higher but costs more tokens is not, so H_T can score below
  the best candidate measured. In `luna-rrsi-p1` and `-p4` a later candidate was admitted below an earlier one (0.66
  where 0.68 was measured, 0.68 where 0.72 was); in `luna-rrsi-p2` only round 0's 0.65 was ever admitted, and five
  later candidates measured at 0.68 to 0.70 (inside its band, 0.061) were refused for their extra tokens. The engineering
  weights (w_s 100, w_c 2: inside the band the score counts) kept the best candidate measured, 0.67, and its H_T
  scores 0.622 on the test questions, +0.6 [−4.2, +5.5] against the four trainings' mean: one training cannot tell
  the two weightings apart.
- **Tokens per test question.** H_0 173 K; the four H_T 181, 181, 186 and 174 K, the engineering arm's 215 K. Paired
  over the questions (ratio of the totals, 95% bootstrap), `luna-rrsi-p3`'s uses 12% more than H_0 [+2%, +22%] and
  the engineering arm's 24% more [+12%, +38%], its tool run in almost every session; the other three are not
  distinguishable from H_0, nor is the mean of the four (+6.3% [−0.9%, +13.9%]). `luna-rrsi-p3`'s session on UID0193
  stopped at the output limit after 592 s with no token count: that question is left out of its 186 K and of both
  ratios that involve it (171 questions, over which H_0 averages 166 K).
- **No leakage.** No final adapter names a question, a bulletin file, or a training or test answer (checked for
  question ids, file names, whole answers of four or more characters and numbers of three or more significant digits).
- **One failed step.** The engineering arm's first Evolver call failed on an error in the framework's reading of a
  Codex session log (outside this method: a line of a Codex error message that is valid JSON but not an object), so
  its step 2 evaluated H_0 again and round 0 went on from there: 22 steps instead of 21. The four trainings had none.

Spending: per training 2,100 evaluation rollouts (406–431 M tokens) and 148–179 M tokens for the proposer, the search
roles and the smoke rollouts, 5.6–6.6 h with the five side by side, the engineering arm 2,200 rollouts (508 M + 153 M)
and 6.7 h; their test evaluations 10–18 min. The times leave out a pause of 3.7 h (2026-11-02 22:38 to 2026-11-03
02:21 UTC) in which the plan's `PAUSE` file held every new session while the login was moved to another ChatGPT
account; the runs went on where they stood. The first account's weekly window did not move a whole percent in the 31
minutes before the pause; the second's went from 56% to 78% while these runs and another training on the same login
ran (an upper bound on their share). All on the Codex plan, no API key. The code each run used is recorded in
`workspace/state/rrsi/code.jsonl` (commit `859bda9`, a clean tree; upstream `be50316`).

The runs' records (each run's final adapter, per-question test records, the rounds' decisions and its config) are not
kept in the repository. The runs kept no record of the analyst's, the digesters' and the critic's sessions (the
scratch they were written to is cleared at each step); the Evolver now keeps them in each step's output.

### Before the port

The same protocol ran before the Evolver's port onto the [agent layers](../../../docs/framework/agent-layers.md), at
commit `c8afc5e` (2026-11-01): four trainings (`luna-rrsi-r2` to `-r5`) 0.602 [0.538, 0.663] (0.605, 0.581, 0.599,
0.622; sd 0.017) and the engineering arm (`luna-rrsi-eng`) 0.616 [0.547, 0.686], each a training of 21 steps with
`agent.concurrency=6`; earlier, with the Evolver as it was before an independent review of the integration and the
configs' concurrency 10, a first training (`luna-rrsi`, 16 steps) 0.593 and, in its window, H_0 0.378 [0.308,
0.448]. Their acting agent was PR #54's OfficeQA Codex agent with `deploy: adapter` (`ADAPTER.md`
as `AGENTS.md`, every other file of the adapter at the session's root, nothing an `exports.yaml` declares connected)
behind the same chatgpt.com-only proxy, and the proposer and the search roles were Codex sessions the Evolver started
itself; now the acting agent is the framework's Codex agent with its deployment (above), and the proposer and the
search roles are sessions of the `codex` Base Agent with the same settings (a digester's still in the round's rendered
traces). Paired per question, the runs above − those: the four trainings +1.5 [−1.9, +4.8], on the value alone +1.9
[−0.7, +4.7]; the engineering arm +0.6 [−5.2, +6.4]; H_0 −2.3 [−7.6, +2.9]. The review fixed the exploration slot (in
rounds 6–9 of the first training, 22 of variant B's 24 verdicts were the reserved-slot tag check, not the critic's
judgement), proposer sessions that ran from the workspace root, variants that could read each other's session logs,
the critic's diff taken from a git repository inside the candidate, the lack of a check against symbolic links in a
candidate, the digesters' read-only shell commands running outside the run's sandbox, and an unbounded re-hand-back;
none of them changed an admission decision in that run, and its result lies among the four's (−0.9 [−5.7, +3.9]
against their mean).
