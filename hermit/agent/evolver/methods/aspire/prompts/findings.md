<!-- Adapted from ASPIRE's fix-loop subagent prompt (NVlabs/ASPIRE,
.claude/skills/iterative-debugging/subagent-prompt.md, Stage 1 Steps 5 and 6: findings.md and
skill_report.json), section names and rules kept; the shell checks it names are run by the harness. -->

The debugging of this task is over. The selected program is version {selected} ({debug_passed}/{debug_total}
development seeds). Write the report the coordinator promotes reusable findings from. Format (verbatim from
ASPIRE):

  # Findings: robosuite/{suite}/task_0
  ## Root causes observed
  <one bullet per failure mode, with the seeds it cost>
  ## What fixed them
  <the change per mode, with evidence: the development rate before and after>
  ## Generalizable patterns
  <patterns likely to help OTHER tasks — prompts that worked, grasp selection tricks,
   waypoint/transport sequences, push techniques. For EACH pattern give:
   - target skill file: localize.md | grasp.md | transport.md | manipulation.md
   - trigger: the symptom or scene condition that calls for it
   - the exact working code snippet (5–20 lines) copied from your fix, in a fenced code block —
     the coordinator promotes this snippet nearly verbatim; a prose description of code is not
     promotable
   - evidence: which seeds it flipped on this task
   If nothing generalizes beyond this task, write "none".>
  ## Skill report
  <How the shared library actually performed on this task. One line per skill you consulted:
   name it, give a verdict (useful | misleading | wrong | not-applicable), and the evidence.>
  ## Blocked modes
  <mode → root cause, or "none">
  ## Non-privileged API audit
  <PASS, or QUARANTINED with the forbidden dependency>

Then, after the report, give the skill report as one JSON object in a fenced ```json block:

{{"consulted": [{{"skill": "<file.md>", "section": "<section>", "verdict": "useful|misleading|wrong|not-applicable",
  "evidence": "<seeds, what happened>"}}],
 "proposed_edits": [{{"skill": "<file.md>", "section": "<section>", "change": "<concrete change>",
  "why": "<the observation behind it>"}}],
 "proposed_new": [{{"skill": "<file.md>", "title": "<short name>", "trigger": "<symptom or scene condition>",
  "code": "<the working snippet, 5-20 lines, generalized>", "evidence": "<seeds it flipped on this task>"}}]}}

Rules (verbatim): "Write each proposal so a stranger can follow it, and propose only what you have evidence
for. It will be tested: a verifier re-solves this task from your proposal and the library alone — not your
program, not your findings, not your run artifacts. Anything you leave implicit is not in the skill, and a
proposal that fails that test is not promoted." A snippet must run under the program rules (only `import
math`, no names starting with `_`), must not compute with a number it never derives, and must name the
segmentation prompt it depends on. Consulted nothing and propose nothing? Give empty lists.

The development history:

{history}

The selected program:

```python
{program}
```
