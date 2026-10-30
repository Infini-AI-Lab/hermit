# Reproduce a published method

A reproduction brings a published learning method into the framework as a template and an Evolver, runs it on one
of the examples under the settings the other methods use, and documents how it differs from the authors' own
code. This page is the contribution guide for that work: what to settle before writing code, what the pull request
must contain, and in which order to verify. It is written from the reproductions already in the repository
([GEPA](../methods/gepa.md), [SkillOpt](../methods/skillopt.md), [Harness²](../methods/harness2.md),
[Continual Harness](../methods/continual_harness.md), [ReHarness](../methods/reharness.md),
[Meta-Harness Adapter](../methods/meta_harness_adapter.md)).

## 1. What the adapter is and what the Evolver is

State, before writing code, which part of the method is the **adapter** (the learned object: the files the acting
agent reads or runs) and which is the **Evolver** (how evidence becomes an update), and name the template and the
Evolver the reproduction adds.

- The template declares the method's levers as learnable paths and must expose them in the form the method uses:
  if the method's skills are executable, the template carries them as files the acting agent can run (for Codex, a
  `SKILL.md` with a `scripts/` directory), not as prose about skills. A method whose harness is more than one text
  file does not fit `prompt-only`.
- Use an existing template where one fits. A new template says which existing ones were considered and why none
  fits (their structure, their fixed paths, what reaches the acting agent).
- Draw the Evolver's step as a flow chart: what it reads, what it calls (the learner, the tools, the rollouts), what
  it writes, and how the final adapter is chosen. The method page carries it
  ([Meta-Harness Adapter](../methods/meta_harness_adapter.md) has one).
- When the framework narrows what the method may change (the acting agent is fixed, only the adapter changes), the
  method's name says so, and the method page states the difference from the paper in its first paragraph.
- A reproduction changes only three places: `hermit/algorithms/templates/<name>/`,
  `hermit/algorithms/evolvers/<name>/` and an example under `examples/<task>/<name>/`. Anything else in `hermit/`
  is the core and stays untouched; a reproduction that seems to need a core change opens a separate discussion
  first, and that change lands on its own, before the reproduction.
- Task-side adaptations of an acting agent (for example, deploying a memory file into a Codex workspace and
  collecting it afterwards) are application code under `examples/` and are allowed, listed separately in the pull
  request.
- Where the authors publish their code, use it: pin it to a Git commit in a dependency group of `pyproject.toml`
  (`[dependency-groups]`), add the group to `all`, add the same requirements to `METHOD_REQUIREMENTS` in
  `hermit/evolver/dependencies.py` so the missing-dependency error names them, and call the code at its seam, as
  `harness2_upstream` does. When the code cannot be used as is, say so and name, per component, which upstream file
  and function the reimplementation follows.

## 2. The differences from the authors' code

Read the authors' code, not only the paper; the paper omits what the code decides (the Refiner's exact input, the
format of each pass's output, whether a skill is text or a program). The pull request carries one table, **the
authors' implementation against this one**, with a row per dimension and, for every difference, whether it is
forced by the framework, forced by the task, or a decision of the reproduction, and whether it is acceptable or
must still be closed:

| Dimension | What to compare |
|---|---|
| When learning happens | the trigger (a schedule in one episode, every step, on demand), the window the learner reads |
| What the learner sees | the trajectory, tool results, scores, the judge's feedback, reference answers, validation data; what the acting agent sees of the adapter |
| Each component's carrier | text, structured entries with metadata, executable code; who can write it (the Evolver, the acting agent, both) |
| The update | whole rewrite or deltas on named entries; any gate, scoring or selection; rollback |
| Models and budgets | the learner's model, the acting model, effort, turn and time caps, batch or window size |
| What is not ported | loops or features the reproduction leaves out, with the reason |

Keep the method's properties honest: a reproduction that feeds the learner something the authors' code does not
(for example the judge's feedback) must not keep the authors' description of it (for example "label-free"). A
window or batch size that has no counterpart in the authors' setting (their window is fifty actions of one game
episode; ours is a number of questions) is a decision, recorded as one.

Texts the learner reads or writes follow the repository's rules: no dataset facts in learner-facing texts, task
texts informational only, the authors' prompts kept verbatim where they exist and marked where adapted.

## 3. The evaluation protocol

Say which protocol the method is evaluated under, and why, because the two protocols answer different questions:

- **Train / test split.** The Evolver learns on the training split (with or without validation) and the final
  adapter is evaluated frozen on the test split. GEPA and SkillOpt are evaluated this way; so is Continual Harness
  in its `run_codex.yaml`.
- **Streaming.** The test tasks arrive in one order; each task is answered by the current adapter before the
  Evolver learns from it, the adapter is carried to the next task, and the stream's own answers are the result.
  Harness² is evaluated this way, and Continual Harness in its `stream_codex.yaml`; the authors' own setting of
  Continual Harness is streaming (online, reset-free, no held-out tasks).

In the streaming protocol the learner may see each task only after answering it, and the pull request says what
the learner sees of it (with the judge's feedback the stream is online supervision; without it, label-free).

## 4. The first evaluation's settings

We recommend running at least one experiment with GLM 5.3 Flash
(`fireworks/accounts/fireworks/models/glm-5p3-flash`) in both roles, acting and learning: it is cheap enough to
repeat, and it puts the method on the rows the other methods already have.

The first evaluation of a reproduced method runs on the row of settings the other methods already use on the same
example, so that it joins their table. For OfficeQA, as of 2026-10-24:

| Setting | Value |
|---|---|
| Acting model | GLM 5.3 Flash, reasoning effort medium |
| Learning model | GLM 5.3 Flash, reasoning effort medium |
| Harness | Codex in a container per session (`officeqa-codex:0.162.0`), the whole corpus under `docs/` |
| Starting adapter | SkillOpt's initial OfficeQA skill |
| Data | the paper's 50 / 24 / 172 split, or the seed-0 stream of the 172 test questions |
| Evaluation | the 172 test questions, one pass; exact match |
| Baselines on the row | no adapter, the starting adapter, the methods already measured |

State every deviation from the row (a session wall clock that cannot be matched, a harness the task's own code
fixes) in the table header. Record with the result: the framework commit, the complete config, the model
identifiers and effort, the Codex version and image, the concurrency, the dataset revision, the spending and the
time, and the standard error a one-pass evaluation carries (about 3.7 points at 172 questions). Report paired
differences against the starting adapter, not only accuracies.

## 5. Verification, in order

1. Unit tests with a scripted model: every pass or stage of the Evolver, its rollback path, and the template check
   on the template root, a seed and a broken tree.
2. `check_spec` on the template root and `hermit check` on the example's configs.
3. A lite smoke (a few tasks, a budget cap), after which the trajectories are read, not only the scores: each lever
   the method claims (a skill opened, a helper run, a sub-agent spawned, a memory note written and read back) is
   seen in a session, and a failure is traced to a capability or to the answer's form.
4. The first evaluation under section 4, with its run record.
5. A method page under `docs/methods/`: setup, the step, the parameters (a `literalinclude` of `evolver.yaml`),
   what the reproduction is not; a row in `docs/methods/index.md`, the example's `README.md`, and the results in
   the example's recipe.

## 6. The reproduction's pull request

These rules apply to a pull request that reproduces a published method; other pull requests follow
[CONTRIBUTING.md](https://github.com/Infini-AI-Lab/hermit/blob/main/CONTRIBUTING.md) alone.

- Rebase on the current `main` before opening it and after any later change to `main`; resolve conflicts on the
  branch; run the test suite after the rebase.
- Open it as a draft until the first evaluation has run and its row is in the description.
- The description carries the table of section 2, the protocol of section 3, the settings and result of section 4,
  the list of application-side changes, and what is left out. No attribution lines.
- The description answers seven questions, each in a few sentences, with links to the files, runs and records it
  rests on:
  1. **Core changes.** Does the pull request change the core (anything in `hermit/` outside the method's template
     and Evolver)? If so, what changes, and why can the method not work without it? (Section 1.)
  2. **Adapter template.** Does the pull request add a new adapter template, or use an existing one? Which one, and
     if it is new, why does no existing template fit? What may the learner change, what is fixed, and what reaches
     the acting agent? (Section 1.)
  3. **Evolver.** Does the pull request add a new Evolver, or use an existing one? Draw one step as a flow chart (a
     mermaid diagram in the description is enough): what the Evolver reads, what it calls (the learner, the tools,
     the rollouts), what it writes, and how the final adapter is chosen. (Section 1.)
  4. **Experiments.** What was run, and on what? The benchmark and its data (split or stream), the protocol, the
     acting agent and its model, the Evolver and its model, the budgets, the number of runs, and the results
     against the baselines measured on the same settings. (Sections 3 and 4.)
  5. **Documentation.** Which documents were added or changed to match (method page, recipe, example README,
     method index, changelog)? (Section 5, step 5.)
  6. **This description.** Have you read it end to end, and does every path, name, number and claim in it match the
     code and the run records it links to?
  7. **Independent review.** Has an independent agent (a separate session, not the one that wrote the change)
     reviewed the pull request for code quality and potential issues? Say what it found and what was changed.
- Any core change is a separate pull request, discussed before it is written.
