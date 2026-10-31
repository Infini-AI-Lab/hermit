# Continual Harness

Continual Harness ([Karten et al., 2026](https://arxiv.org/abs/2605.09998), [code](https://github.com/sethkarten/continual-harness))
keeps a harness H = (p, G, K, M), the base policy, sub-agents, skills and memory, and refines it while the agent acts:
a Refiner reads a window of the agent's own trajectory, identifies failure signatures in it and emits per-component
edits that are applied in place, H <- H + delta, with no accept/reject gate and no score. Four passes per generation,
one model call each: the policy is rewritten whole; sub-agents, skills and memory are edited as create / update /
retire deltas on named entries; the memory pass is conservative, because the agent writes memory itself while acting.

## Setup

```yaml
template: continual-harness@1.0
data: {train: train.txt, step_size: 3}                              # the Refiner's window: a step's rollouts
agent: {name: ../utils/agent/codex_agent.py:OfficeQACodexAgent}      # an agent that runs `codex exec` with the tree deployed
evolver:
  name: continual_harness
  params: {feedback: true}
llm: {model: <backend>/<model>}                                     # the Refiner's model
```

The template is the harness state in the layout Codex reads from its workspace root: `AGENTS.md` (p; `ADAPTER.md`
is its copy), `sub_agents/<name>/AGENT.md` (G, installed as Codex agent roles), `.agents/skills/<name>/SKILL.md`
with executable helpers under `scripts/` (K) and `memory/NOTES.md` (M, notes under `## <topic>` headings that the
acting agent reads and writes during a task). The acting agent deploys the tree into its workspace and hands the
memory it wrote back with its trajectory ([`codex_agent.py`](../../examples/officeqa/utils/agent/codex_agent.py)).
The template's `exports.yaml` declares the skills and the subagents, so `hermit deploy` connects them to the host;
the rest of the tree, the memory included, is in the workspace's copy under `hermit/continual-harness/`.

## The step

1. The Trainer rolls the current harness out on the step's tasks.
2. The Refiner reads each trajectory (a Codex event stream, or a message list), its final output and status, and
   extracts failure signatures: a session that erred or timed out, a command that exited non-zero, an answer that
   never came; with `feedback: true` (the default) the judge's verdict on each task scored below 1 is shown too, as a
   signal to act on, not a score to optimise. With `feedback: false` the Refiner sees the trajectories alone, the
   paper's own setting, where no judge exists.
3. Notes a session left in `memory/NOTES.md` are folded into the harness's memory (a known topic is replaced, a new
   one appended).
4. Four passes, in order, each one call of the run's `llm` tool: the policy (`AGENTS.md`, rewritten whole, at most
   8,000 characters), the sub-agents (create / update / retire), the skills (add / update / retire, with helpers)
   and the memory (at most `memory_max_changes` notes). Each pass is applied to the adapter and checked with
   `check_spec`; a pass whose result fails the check is dropped, the others stand.

Nothing is scored and nothing is selected: the harness after the last step is `final`. `state/continual_harness.json`
logs every step; each step's `output/report.md` says what each pass changed.

## Parameters

```{literalinclude} ../../hermit/algorithms/evolvers/continual_harness/evolver.yaml
:language: yaml
```

## What it is not

The paper's second loop, co-learning, updates the model's weights from a teacher's process rewards; it is not part
of this Evolver. The paper's Refiner sees no external feedback; `feedback: true` is a decision of this port that
puts Continual Harness on the same label footing as GEPA and SkillOpt (the training questions' verdicts), the gate
being what still sets it apart. Harness² ([harness2.md](harness2.md)) uses no labels at all.

## Example

[`examples/officeqa/continual_harness/`](https://github.com/Infini-AI-Lab/hermit/tree/main/examples/officeqa/continual_harness): OfficeQA in the SkillOpt
paper's setting, Codex as the acting agent, GLM 5.3 Flash acting and refining. Its `stream_codex.yaml` is the
paper's own protocol (one stream over the test questions, `feedback: false`): on OfficeQA's 172 test questions in
the same order as the Harness² stream it scores 0.634, against 0.576 for the initial skill it starts from (+5.8
[−1.2, +12.8] paired) and 0.651 for Harness² (−1.7 [−7.6, +4.1]), at about a quarter of Harness²'s cost
([the example's result](https://github.com/Infini-AI-Lab/hermit/tree/main/examples/officeqa/continual_harness#result), [the recipe's Flash row](../recipes/officeqa.md#the-flash-row)).
With Codex and GPT-5.6-Luna acting and refining on a ChatGPT login
([the Flash row with GPT-5.6-Luna](../recipes/officeqa.md#the-flash-row-with-gpt-56-luna)) the same stream scores
0.477 against 0.390 for the initial skill (+8.7 [+1.7, +15.7]) and 0.611 for Harness² (−13.4 [−20.3, −6.4]): what
that model gets wrong is mostly the answer's form, which leaves a clean trajectory, so without the verdict the
Refiner has little to see.
