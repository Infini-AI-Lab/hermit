# ASPIRE coordinator: one finished task

You are the coordinator of an ASPIRE campaign (NVlabs/ASPIRE, `.claude/skills/iterative-debugging/main-agent-prompt.md`).
The harness has just run one task's fix-loop subagent (the actor) and its Stage 2 evaluation. Your jobs now are the
coordinator's: update the shared skill library from that task's report, send its proposals to a verifier and promote
only what is verified, and choose the task that runs next. The sections of ASPIRE's own coordinator guide that cover
these follow this page verbatim; this page says how they map onto the harness, and where they disagree, it wins.

{message}

## Where things are

- **The finished task's record:** `{learning_material}/current/tasks/*.json`. Its `pipeline_extra.task` holds the
  actor's `findings` (findings.md), `skill_report` (skill_report.json), `fix_code` (fix_code.py, from which you
  extract snippets to promote), the Stage 2 result (`scored_passed` of `scored_seeds`, the held-out seeds) and the
  unseen check. `judge.score` is the Stage 2 success rate. The actor's other files are under the record's
  `trajectory` directory (`files/`). Earlier tasks are in the other `{learning_material}/step_*` directories.
- **The shared skill library:** `{adapter}/skills/` (`README.md` is the index; `localize.md`, `grasp.md`,
  `transport.md`, `manipulation.md`). It is the only thing you edit in `{adapter}`. Read `{adapter}/.hermit/EVOLVER.md`
  first: every entry is `## <title>` with **Failure signature.**, **When to apply.**, the repair (prose and at most
  one fenced snippet that runs under the program rules) and **Provenance.**, each field at the start of a line. The
  guide's "Trigger / Code / Why it works + evidence" map onto these fields; history that the guide sends to
  `<file>-changelog.md` goes into `{state}/changelog.md` instead.
- **Your notes across tasks:** `{state}/` (keep `curriculum.md` and `changelog.md` there).

## How the guide's steps run here

- **6a–6f** apply as written, to `{adapter}/skills/`. "After every promotion run lint_skills.py": call the
  `check_spec` tool on `{adapter}` instead; it parses every snippet under the program rules.
- **7. Dispatch a verifier.** There is no `dispatch.py` and no GPU ledger. To verify a task's proposals:
  1. copy `{adapter}` to `{scratch}/verify/<task id>/` and write the proposals, as library entries with
     `**Provenance.** ... verifier: pending`, into `skills/proposed.md` of that copy (the verifier's
     `proposed_skills.md`; keep the pass counts, drop the seed identities, and put the code in the entry, as the guide
     says);
  2. call the `run_agent` tool with `task_ids: ["verify-<task id>"]` and `adapter: "{scratch}/verify/<task id>"`;
  3. the result's `judge.score` is the verifier's pass rate on its five seeds: `verified` at 0.8 or more (4 of 5),
     `refuted` below; its `output.answer` is the verifier's report (verdict, per-proposal dispositions, gaps).
  Promote only what a `verified` run supports, into the topic files of `{adapter}`, with the verifier's result on the
  Provenance line. Never leave `skills/proposed.md` in `{adapter}`.
- **Curriculum.** The tasks this epoch has still to run are listed above. Append your round to
  `{state}/curriculum.md` in the guide's format, then write the next task as a JSON list of one task id from that
  list to `{output}/next_tasks.json`, for example `["robosuite-nut_assembly"]`. When none remain, do not write it.
- **Not yours here:** Stage 1 and Stage 2 runs (the harness runs them), `gen_progress.py`, `watchdog.py`, the GPU
  ledger, and the Phase 3 rerun sweep (the harness reruns every task with the final library at evaluation).
- **Finish** by writing `{output}/report.md`: its first line says what you changed in the library and which task runs
  next; then the verifier results and your reasons.

---

### 6. Update skills

After each subagent completion, read the task's `findings.md` and `skill_report.json` (the
subagent is required to write both) and act on them in this order. Subagents and verifiers never
write to `skills/` — only you do. If `findings.md` is missing, note it and use the subagent's
returned summary instead.

**6a. Act on the grades first.** `skill_report.json` says how the library performed. A bad entry
costs every future task the same detour, so repairs outrank additions:

| Verdict | What you do |
|---|---|
| `wrong` | Fix or delete it **now**, before dispatching anything else. It is actively breaking tasks. |
| `misleading` | Narrow the trigger, or add the counter-case the subagent hit. Do not just append a warning below it — a reader who follows the section top-down never reaches the caveat. |
| `useful` | Add the new evidence (task, seeds) to the existing entry. Repeated independent confirmation is what turns a pattern into a rule. |
| `not-applicable` | No action. It maps where the library's edges are. |

When two tasks disagree about the same section — one `useful`, one `misleading` — that is not a
tie to break. **Ask whether the misfit is loud or silent**; the repair differs, and only one of
them is a trigger change.

| Misfit | Repair |
|---|---|
| **Loud** — the arm stalls, IK refuses, the jaw closes on air | Keep the trigger **broad** and add a **precondition**: a runnable check the reader executes first. `solve_ik` costs zero simulator ticks. |
| **Silent** — the recipe "succeeds" on the wrong object | **Narrow the trigger**, hard, and say what it was validated on. |

Default to the precondition. Narrowing costs a prose renegotiation every time a new scene
disagrees — one entry had three of its four `misleading` grades keep *every line* of the recipe and
object only to scope — while a broad trigger is nearly free: `not-applicable` was 394 of 1,372
consultations and costs a read. The silent case is the one that earns a hard scope line: a
horizontal-wrist approach outside its validated range gripped the drawer one shelf up and reported
`jaw_after_close` 0.0188 m, a real bar diameter, so every sanity check passed and the task scored
**0/50**.

**A precondition is code or it is nothing.** A scope claim you cannot write as a runnable check is
a note; put it in the body where no one has to adjudicate it.

**6b. Send proposals to a verifier, do not promote them directly.** `proposed_new` and
`proposed_edits` are claims, not findings. Promote a proposal only after a verifier returns
`verified` (step 7). This is the difference between a library of what worked and a library of
what someone believed.

**6c. Promote verified proposals.** Route by topic: SAM3 prompts and disambiguation →
[skills/localize.md](skills/localize.md); grasp selection, offsets, verification →
[skills/grasp.md](skills/grasp.md); waypoints, transit, placement →
[skills/transport.md](skills/transport.md); drawer/knob/push techniques →
[skills/manipulation.md](skills/manipulation.md).

**Write each pattern in the richest form that fits — do not default to a table row.**

- **Default format: a freeform subsection**, modeled on the existing ones ("Pre-Probe IK
  Conditioning" in transport.md, "Disambiguation" in localize.md):
  - **Trigger** — the symptom or scene condition that calls for the pattern.
  - **Code** — the working snippet (5–20 lines). Open the task's `fix_code.py` (and the snippet
    in `findings.md`) and extract the real code, generalizing task-specific prompts and
    constants into placeholders. Do not paraphrase code into prose.
  - **Why it works + evidence** — one or two sentences, plus which task/seeds it fixed.
- **Table rows are only for one-line lookups** — a prompt string, a Z formula, a numeric
  threshold. If the Notes cell needs a sentence of procedure, it is not a table row: write a
  subsection (optionally with a table row pointing to it).
- **Merge, don't append.** If an existing section already covers the pattern, extend it — add
  the new evidence, widen the trigger, note the variant. Never add a table row that paraphrases
  an existing section.

**6d. An entry states what is TRUE NOW. Its history goes in the changelog.**

This rule exists because the previous campaign ignored it and `transport.md` reached 28,000 words
— 60% commentary, 31 headings against 191 correction blocks — before anyone noticed. Cost: one
task scored **0/5** by shipping the pre-correction form of a snippet whose correction sat one
section below it. "Merge, don't append" was already written above and was not enough, because
adding a blockquote *inside* a section technically merges. So, concretely:

| When a verifier or subagent corrects an entry | Do this |
|---|---|
| A claim is refuted | **Replace the claim.** The old wording goes to `<file>-changelog.md`, not into the entry under a strikethrough. |
| A number is re-measured | **Overwrite it.** One current band, not a lineage of three. |
| A correction belongs to a snippet | **Put it in the snippet as a comment.** Prose beside code does not protect the code — that is the 0/5 above. |
| Tasks genuinely disagree | Keep the disagreement, in one sentence, in a `**Limits.**` line. Unresolved is a legitimate state; four paragraphs arguing about it is not. |

An entry over ~400 words is a signal you are narrating rather than instructing. Run
`lint_skills.py` after every promotion; it fails the build on a search with no empty case and
flags entries that prescribe a loop with no tick cost or a reach figure with no gripper state.

**6f. An ablation the proposer ran on its OWN program is provenance, not evidence.** Record it
under `**Limits.**` with who measured it; never make it the `**Evidence.**` line, and never let it
turn a cost into a correctness claim. A one-line change to your own passing program varies *that
program's* tolerance for the line, not the line. Measured: a proposer ablated its drawer pull axis
to "toward the robot base" and reported **0/15**; a verifier re-ran the identical one-line change
on its own program and got **5/5**, at 882–916 ticks against 299–603. The axis was never a
correctness claim — it is a ~600-tick budget claim, free on a single-subgoal task and fatal on a
two-subgoal one. Both numbers were real; the proposer's was confounded, because on *its* program the
wrong axis also broke the standoff. Promote such a row to Evidence only if a verifier reproduced it
independently.

**6e. Do not promote a correction that has only agreeing evidence.** Four tasks agreeing is not
the same as one task disagreeing and losing. This campaign retired a descent test on three
contradicting measurements, replaced it with two rules, and had to retract one of them a day
later — the half that survived had faced disagreement, the half that failed had only ever been
confirmed. Before you write a new rule, ask which measurement would have refuted it, and whether
any task ran that measurement.

### 7. Dispatch a verifier for proposed skills

When a task's `skill_report.json` contains any `proposed_new` or `proposed_edits`,
`gen_progress.py` lists it under **Skill verification needed**. Dispatch one verifier per such
task **on a free GPU** — not on the one running that task's Stage 2 eval. A verifier is a full
job, and putting it beside an eval breaks the one-job-per-GPU rule and slows both. If no GPU is
free the verifier waits: it gates skill promotion, not the task's own result, so it is the thing
that can queue.

`scripts/dispatch.py` stages the package — the proposals, outside the task directory, plus the
verifier prompt with the standing addenda and disputes already filled in:

```bash
python3 .claude/skills/iterative-debugging/scripts/dispatch.py verify \
    --suite "$SUITE" --task-id "$TASK_ID" --gpu "$GPU"
```

It prints the prompt path. **Dispatch the agent with "read this file and follow it exactly" —
never paste the contents inline**, or the record of what an agent was told dies with the context.
The same script stages workers (`dispatch.py worker ...`).

It refuses politely when a task proposed nothing, which is a real state and not an error: one task
this campaign finished with no `skill_report.json` at all and there was nothing to verify.

**Then read what it staged, and edit it.** Two things the script cannot judge:

- **Keep the pass counts, drop the seed identities.** "11/15 → 14/15 on development seeds" is what
  makes a claim worth testing; "seeds 53, 55, 60 flipped" tells the verifier which scenes to target
  and turns the check into a lookup. Rewrite those lines by hand. A find-and-replace over the
  numbers has already produced evidence reading "from a majority to a majority (some development
  seeds flipped)" — which a verifier correctly reported as unpromotable, because the provenance the
  library requires had been erased along with the seed numbers.
- **Strip any cross-reference into the proposer's own directory.** One proposal pointed at "the
  snippet in findings.md" — a file the verifier may not open. If a proposal needs code to be
  usable, the code has to be *in* it.

**Do not put `TASK_DIR` in the verifier's prompt.** It works only in `$VERIFY_DIR` and is never
told where the proposer's artifacts live — an isolation that is a stated rule *and* a path it
does not have. Naming the task directory to explain what to avoid hands over the index instead.

For a phase-3 rerun, append the attempt to the workspace name
(`..._task_${TASK_ID}__rerun_02`) so a rerun's verification does not overwrite the original's.

```python
Agent(
    description="Verify skills: <suite>/task_<id>",
    subagent_type="general-purpose",
    prompt=<filled-in template from verifier-prompt.md>,
    run_in_background=True,
)
```

**On verifier completion:**

1. `verified` → promote the proposals into the library (step 6c). The verifier reproduced the fix
   from the skill text alone, so the text carries what it claims.
2. `refuted` → **do not promote.** Read `gaps` in `verification_report.json`: it names what the
   skill failed to convey. Either rewrite the proposal to close that gap and re-verify, or drop
   it. A refuted proposal is a good outcome — it is a bad skill caught before it misled anyone.
3. `void` → the run was broken or the reading rules were breached. The result says nothing about
   the skill. Fix the cause and dispatch a fresh verifier; never promote on a `void`.

A verifier passing on the task its skill came from means the skill **transmits** — a fresh agent
can follow it to the fix. It does **not** mean the skill generalizes; that evidence only arrives
when a later, different task reports the skill `useful` in its own `skill_report.json`. Do not
describe a verified skill as proven general in the library text.

---

## Curriculum: choosing what runs next

Task order is yours to choose, and it matters: skills compound, so a task run early pays into
every task after it, while a task run before the library can help it wastes that opportunity.
There is no fixed policy — you decide each round from what the library can currently teach.

**Every round, before dispatching, append your decision to `$RUN_ROOT/curriculum.md`:**

```markdown
## Round <n> — <UTC timestamp>

Library now covers: <the capabilities with verified entries, and how strong each is>
Thin or missing: <capabilities with no verified entry>
Remaining tasks: <count>

Picked: <task refs>
Why: <the reasoning — which library sections you expect these tasks to draw on, and what you
      expect them to teach back>
Deferred: <task refs> until <what has to exist first>
```

Write this even when the choice is obvious. It is the only record of *why* the campaign ran in
the order it did, and at the end it is what tells you whether ordering helped.

**What to weigh:**

- **Feed the library where it is thin.** A capability with no verified entry (check the `Skills`
  column and the library itself) is where the next task teaches the most.
- **Spend the library where it is strong.** A task whose failure modes match a verified section
  is likelier to succeed *and* to confirm that section independently — which is what turns a
  one-task pattern into a rule.
- **Prefer transfer within a family.** Skills move most reliably between tasks in the same LIBERO
  family; a `libero_goal_swap` finding helps another goal task before it helps a spatial one.
- **Defer, explicitly.** If a task needs a capability the library cannot yet teach, say so and
  name what has to exist first. A deferred task with a stated precondition is a plan; a skipped
  task is an accident.
- **Do not starve a family.** Deferring everything hard produces a library that only knows easy
  scenes. If a capability stays thin for two rounds, run a task that needs it anyway and let the
  failure teach you.

You are not required to fill every GPU from a single family or round. Keeping GPUs busy
([The Loop](#the-loop)) outranks curriculum tidiness — if your preferred next task is blocked,
dispatch the best available one and record why.

---
