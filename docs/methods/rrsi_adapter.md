# RRSI Adapter

RRSI Adapter is RRSI (Xia et al., 2026, [arXiv:2609.24972](https://arxiv.org/abs/2609.24972);
[code](https://github.com/google-research/rrsi)), regularized recursive self-improvement of agent harnesses, with the
harness confined to the adapter, as [Meta-Harness Adapter](meta_harness_adapter.md) confines Meta-Harness. Each round
an analyst reports the incumbent's failures from its own evaluation; m proposer sessions edit the incumbent under an
annealed edit budget, with the edit history, an exploration slot when the run stalls, and a list of components to
prune; a critic reads each diff before any evaluation is spent; each screened candidate is evaluated on the whole
evolve set, and replaces the incumbent only above a noise-adjusted floor and within a cost rule. Choose it when you
want harness evolution that resists overfitting its own evolve set and keeps the harness's token cost in check, and
you have the budget to evaluate every candidate on the whole training split with k trials per task.

**What it is not.** RRSI evolves the harness program itself (in its coding instance, a fork of Terminus-2: its loop,
parsers, prompts and tools). Here the acting agent and its host are fixed, and the edits are to the adapter: the
instructions, and the skills, roles, hooks and helper files the agent receives. Results of this method are results
of RRSI's search over adapters under a fixed agent, not of RRSI itself.

## Setup

```yaml
template: rrsi-adapter
start_from: seed_adapter         # H_0
evolver:
  name: rrsi_adapter
  params:                        # Layer 3: the method
    domain: domain.py:MyDomain   # the benchmark's texts and rules (an AdapterDomain)
    rrsi: rrsi.json              # RRSI's hyperparameters (upstream's RRSIConfig fields)
  base: codex                    # Layer 2: the proposer is a Codex (or Claude Code) session; the roles, Codex sessions
  model: {name: gpt-5.6-luna, login: chatgpt, effort: medium}   # Layer 1: the proposer's model (and the roles')
data:
  step_size: 178                 # the whole evolve set (k rows per task, `<task>#<k>`) per step
  shuffle: false
trainer:
  epochs: 45                     # 1 + T x m = 41 (H_0, then each candidate once), and room for a step again
  allow_early_stop: true         # the Evolver ends the run after round T-1
```

It needs the `rrsi` dependency group (`python -m pip install -e . --group rrsi`): upstream's package at a pinned
commit, whose code runs unchanged for every rule and for the analyst and the critic. The analyst, its digesters and
the critic are Codex sessions with the proposer's model, login and effort, or with `evolver.params.roles`' (which a
Claude Code proposer needs).

## The template

`rrsi-adapter` fixes only `.hermit/`, as `meta-harness-adapter` does: every other file of the adapter may be added,
changed or removed, with no declared structure, check of its own or exports. Its guide, `EVOLVER.md`, says what an
adapter is, that `ADAPTER.md` is the agent's instructions at the start of every task and that anything else reaches
the agent only if it is told what is there, and names RRSI's component vocabulary K (`prompt`, `control_flow`,
`output_plumbing`, `context_mgmt`, `config`, `client_tool`, `skill`, `memory`, `subagent`) with what an edit of each
must show in the diff, because the tag a proposer gives an edit is checked against the diff. No existing template
names the vocabulary, and the method's history, exploration and pruning are kept per component.

## One framework step

One step evaluates one candidate (`data.step_size` covers the evolve set, k rows per task, so a step is an epoch):

```mermaid
flowchart TD
    R["Trainer: the rollouts of the handed-back candidate on the evolve set"]
    G{"The working copy that ran is the stored candidate (digest),<br/>and few enough trials are missing?"}
    E["1. Record S and C against that candidate<br/>(step 1: H_0, and delta from upstream's calibrate)"]
    A1["Evaluated again at the next step (once),<br/>then counted invalid"]
    D{"Every candidate of round t measured?"}
    S["2. Decide round t with upstream's select_round:<br/>floor S* - delta, cost rule, in-band score; history, attribution, frontier"]
    T{"Rounds left?"}
    O["3. Open the next round: upstream's analyze (digesters, analyst) on the incumbent's traces;<br/>edit budget b_t, stall flag, exploration slot over untried components the agent can carry, prune set"]
    P["Per variant (m of them): a proposer session edits a copy of the incumbent in its own directory<br/>and writes done.json; upstream's done() contract is checked, problems go back to the session"]
    C["Upstream's critic on the staged diff (precheck patterns, then review), up to five repairs;<br/>tags normalized from the diff"]
    K["check_spec, then the smoke: run_agent on the domain's smoke tasks"]
    Q["Stored with a digest and queued"]
    X["Refused: recorded, no step spent"]
    H["4. Hand back the next queued candidate<br/>(none queued: the round is decided at once and the next one opened)"]
    F["The incumbent is handed back, the step returns stop;<br/>finalize hands back H_T"]
    R --> G
    G -- yes --> E --> D
    G -- no --> A1 --> H
    D -- no --> H
    D -- yes --> S --> T
    T -- yes --> O --> P --> C
    C -- passed --> K
    C -- refused --> X
    K -- passed --> Q --> H
    K -- failed --> X --> H
    T -- no --> F
    H -- "the next step evaluates it" --> R
```

1. The step's rollouts are the evaluation of the candidate handed back last time (the starting adapter, H_0, on the
   first step). They are credited to it only when the working copy that ran has the stored candidate's digest; an
   evaluation with more missing trials than `invalid_missing_frac` (or one the Trainer repaired) is run once more,
   then counted invalid, as upstream does. H_0's evaluation seeds the frontier, and delta is upstream's `calibrate`
   on it unless the config gives one.
2. When every candidate of the round is measured, upstream's `select_round` decides it: a candidate is admissible
   only above S* - delta and within the cost rule, and the best admissible one becomes the incumbent; the history
   L_t, the attribution of each edit's predictions and the frontier are upstream's records.
3. While rounds are left, the next one is opened: upstream's analyst reads the incumbent's own evaluation (each
   task's trajectories rendered, digested per task, then ranked into failure modes, capability gaps and habits to
   keep), and upstream's schedule and history give the edit budget, the stall flag with its reserved exploration
   slot, and the components to prune. Each of the m variants is a proposer session on a copy of the incumbent; its
   `done.json` must meet upstream's `done()` contract, its diff must pass upstream's critic (with up to five repairs
   in the same session), its tags are normalized from the diff, and it must pass `check_spec` and the smoke.
4. The next stored candidate is handed back and the next step evaluates it. A refused candidate costs no step; a
   round with none left is decided at once. After round T-1 the incumbent is handed back, the step returns `stop`
   (`trainer.allow_early_stop` ends the run), and `finalize` hands back H_T.

## Agent layers

The method's roles on the [agent layers](../framework/agent-layers.md):

| Role | Layer 3 | Layer 2 | Layer 1 |
|---|---|---|---|
| The acting agent | the benchmark's Acting Agent (OfficeQA: the framework's Codex agent with OfficeQA's task functions) | `codex` | the run's `agent.model` |
| The proposer | this Evolver: its prompt, the variant's working directory, what is hidden (`Limits`), `done.json` and the repairs (the session resumed) | `codex` or `claude`, `make(backend, settings).run(...)` | `evolver.model` |
| The analyst, its digesters and the critic | upstream's code; its `generate` is one session per call (`roles.py`), in an empty directory or, a digester's, in the round's rendered traces, with Codex's own sandbox read-only; the digesters' read-only shell under the run's sandbox | `codex`, `make("codex", settings).run(...)` | `evolver.model`, or `roles` |
| The smoke | the Trainer's `run_agent` tool | the acting agent's | the acting agent's |

What the method reuses rather than writes: the sessions' settings are `AgentEvolver.settings` (the login, its plan
and shared copy, the CLI's tools, the cancel), with a home and an event log of each session's own (the variant's, kept
across its repairs, which resume the session; a role call's); their usage is the Base Agent's `Result.usage`; a
session the plan held back, or the provider could not serve, is told by the Base Agent's result; what a session must
not see is the call's `Limits.hidden`, with the run's sandbox on (with it off nothing is hidden: the proposer, the
roles and the digesters' shell run as every learner of such a run does). The traces the analyst and the proposer read are rendered from the framework's
trajectory steps (`hermit.agent.base.trajectory`). The roles are not calls of the run's model (`ctx.agent`): for a
model only Codex serves, the Trainer's chat endpoint runs each call in an empty workspace of its own, where a digester,
told its trace is in its working directory, finds none (in a smoke through the endpoint, 54 of the digesters' 86
replies were no action but a note that the trace was not there, and the round's roles took 93 calls; with the
digesters' sessions in the traces, 17).

## How a run maps onto the Trainer

| RRSI (upstream) | Here |
|---|---|
| `Evaluate(H', D_evolve, k)` | the next step's rollouts: the step's rows are the evolve set with k rows per task, credited to the candidate handed back last only when the working copy that ran is the stored candidate (its digest) |
| baseline and delta (`baseline`, `calibrate(["base"])`) | step 1 evaluates H_0; delta is upstream's `calibrate` on that evaluation when the config gives none |
| `analyze` (analyst and digesters) | upstream's code; its `generate` is one session of the `codex` Base Agent per call, the digesters' sessions in the round's rendered traces |
| proposer (a JSON-action Claude agent, `done()`) | a session of the `codex` or `claude` Base Agent on a copy of the incumbent adapter, writing `done.json`; upstream's `done()` contract is checked and every problem goes back to the same session |
| critic with five repairs, tag validation | upstream's `review` and `normalize`; each objection goes back to the same proposer session with upstream's repair text |
| liveness smoke | `check_spec`, then `run_agent` on the domain's smoke tasks |
| `select_round`, history, attribution, frontier | `select_round` and the history are upstream's code, unchanged; the attribution and the frontier are ported from upstream's driver (H_{t+1}, S* and the trajectory in `state/rrsi/frontier.json`) |
| the result H_T | `finalize` hands back the incumbent |

A run takes 1 + (evaluated candidates) steps, 1 + T x m when every candidate is evaluated once and no step fails;
give `trainer.epochs` room beyond that. A candidate the critic, the check or the smoke refuses costs no step; a round
whose candidates all fail is decided at once and the next is opened in the same step. An evaluation with more missing
trials than `invalid_missing_frac` is run once more, then counted invalid, as upstream does, and a step whose Evolver
call fails is a step too (so is one whose proposer session exits non-zero without a proposal; the variant is drafted
again at the next step, and so is one the Trainer's clock ended); three failed steps in a row stop the run, as
upstream's driver does, at the step after them (the Trainer replaces the result of a step its clock ended, so that
step cannot stop the run itself; a Trainer without `allow_early_stop` runs on, and the count starts again), and
`finalize` then says that the run ended before round T. A step the model's plan
or provider held back within the Trainer's clock (a proposer's or a role's sessions alike) is not counted; a budget
stop ends the run anyway.

## Parameters and defaults

```{literalinclude} ../../hermit/agent/evolver/methods/rrsi_adapter/evolver.yaml
:language: yaml
```

## Where this integration departs from the paper

| Dimension | The paper | Here | Kind |
|---|---|---|---|
| What the proposer may change | The whole harness program | Every file of the adapter outside `.hermit/`; the acting agent, its loop and the host stay fixed | forced by the framework |
| The proposer | Claude Opus 4.8 in upstream's JSON-action loop | A session of the `codex` or `claude` Base Agent with `evolver.model`, upstream's system text, context sections and done() contract | a setting |
| The search roles | Claude Opus 4.8, one completion per call; the analyst and the critic answer from the text they are given | The proposer's model (or `roles`), one Codex session per call with Codex's own sandbox read-only: the analyst and the critic in an empty directory, a digester in the round's rendered traces, which it may read with its own tools as well as through upstream's actions; none of them sees the run's records, the step's output, the proposers' directories or the other role calls' | a setting |
| Component vocabulary | Signals in the Terminus-2 source | The same K, with signals for adapter files (`domain.ADAPTER_SIGNALS`) | forced by the framework |
| delta | Measured from repeated base evaluations (coding: 0.017) | Upstream's `calibrate` on H_0's one evaluation unless the config gives one (its within-task bootstrap reads low by about sqrt(2) with two trials, as upstream's does; on OfficeQA the spread of H_0's own evaluations across the runs, sd 0.035, gives a band of about 0.10 for a difference, against a calibrated 0.045–0.061) | matched to upstream's driver |
| Unexplored components U_t | K minus the components with a measured edit | The same, without the components outside the domain's carriers (`AdapterDomain.carriers`, for what the benchmark's agent cannot carry; OfficeQA's: the five its reported runs could carry, whose deployment connected no hooks and no roles, kept until a run measures the rest): a reserved slot never asks for one | forced by the setting (OfficeQA: a decision, the runs' setting) |
| The proposer's isolation | Its own worktree; its JSON actions reach only the harness and the round's traces | Its own working directory (the candidate and the round's context) and home; with the run's sandbox on, the learning material, the state, the history, the working copy, the step's logs, the digesters' copy of the traces and every other variant are the call's `Limits.hidden`, which the run's sandbox covers or the Base Agent refuses the call | matched |
| Candidate files | The worktree's git diff | The diff between the incumbent's stored files and the staged candidate (without byte code, caches, nested repositories and what git would not commit; every file as text); symbolic links are refused, as in Meta-Harness Adapter, and a file the candidate adds or changes must be UTF-8 text (upstream's proposer writes text files only), both sent back to the session | forced by the framework |
| The critic's precheck | Upstream's patterns and the domain's | The same, plus, for OfficeQA, the training answers the judge's feedback has revealed (whole answers and their longer numbers) and Python an `exports.yaml` names (it would run outside the agent's container) | the reproduction's |
| The digesters' read-only shell action | Runs on the host, in the round's rendered traces | Runs under the run's sandbox with the workspace's records covered, in the copy of the round's traces the digesters' sessions read | the reproduction's |
| The liveness smoke | Two fixed tasks once each; any trial that ended with an exception (a timeout included) fails it | `check_spec`, then the domain's smoke tasks through `run_agent`, a failed rollout run once more before the candidate is refused; OfficeQA's: two training questions the incumbent answered right in every trial, failed by a rollout that timed out or whose CLI exited non-zero, where `run_agent` reports them, and otherwise by any rollout without an answer (a final message without `<answer>` included); a wrong answer passes | the reproduction's |
| A proposer call that fails | An exception in the proposer's model call stops the round, and the driver's next rounds stop at once, so three failures end the run quickly; a call that ends without edits is `no_proposal` | A session that exits non-zero without a proposal fails the step (counted toward `max_failures`), and the variant is drafted again at the next step, each retry costing that step's evaluation of the unchanged adapter; a session that ends without a proposal is `no_proposal` | forced by the framework (the Trainer evaluates at every step) |
| The domain's texts | The coding and workspace domains' constitution, pattern library and role briefs | OfficeQA's, adapted from them: its pattern library's first entry, the analyst's and the critic's briefs and the constitution name the answer's form (the value alone, in the unit and precision asked) as a lever, which upstream's coding and workspace libraries do not (its engineering domain's opens with the analogous "Submission-contract compliance") | the reproduction's (see the example's README) |

## Records to read after a run

| Path | Contents |
|---|---|
| `step_NNNN/output/report.md` | The step: the evaluation recorded, the round decided or opened, the candidate handed back |
| `step_NNNN/output/r<t><v>/session_<n>.jsonl` | The proposer's sessions of a variant |
| `step_NNNN/output/rrsi_roles/<n>/` | Each call of the analyst, a digester or the critic: its prompt, system text, events, reply and `meta.json` (exit code, timeout, time) |
| `workspace/state/rrsi/` | Upstream's files: `frontier.json`, `calibration.json`, `history.jsonl` (L_t), `attribution.jsonl`, `r<t>/` (the analysis, `directives.json`, each variant's proposal, critic verdicts, diff, smoke, and `decisions.json`) |
| `workspace/state/rrsi_adapter.json` | The pump: phase, round, queue, every candidate with its digest and evaluations |
| `workspace/state/candidates/<id>/adapter/` | Each stored candidate |

## Examples

`examples/officeqa/rrsi_adapter/` runs it on OfficeQA with GPT-5.6-Luna acting and learning through Codex (the luna
column of the example's results): ten rounds of two candidates on the 50 training questions, two trials each. Over four
trainings the final incumbent scores 0.616 on the 172 test questions (0.576–0.640), from 0.355 for its starting adapter
in the same harness: level with GEPA and Harness² on that column and above SkillOpt (ACE's 0.640,
the column's highest cell, is not paired with it); as for the column's other methods that were checked (all but ACE),
almost all of the gain is the answer's form, a lever OfficeQA's domain texts name (the README says how). The runs before
this Evolver's port onto the agent layers scored 0.602 (paired, the runs on the port − those: +1.5 [−1.9, +4.8]); the
example's README has both and what the port changed.
