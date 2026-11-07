"""Evo-Harness's prompts (A-EVO-Lab/a-evolve, release/evo-harness, 3c7c7b8, evo_harness/terminal_bench.py: the release's
task-neutral wording of the pipeline; its SWE-bench and CL-Bench prompts name their domains). PROPOSE_FAIL is verbatim;
CURATOR and GENERAL_CURATOR drop the word "terminal"; ATTEMPT and the two texts of the rendered block are this port's.
"""

# This port's: upstream continues the solver's own conversation, which a finished Codex session cannot do from here.
ATTEMPT = """\
You attempted the task below.

## Task
{question}

## Your final answer
{predicted}

"""

# Adapted from swe_bench.py's build_system_prompt (its --lazy-load list), with the file in place of read_skill(name).
SKILLS_INTRO = ("You have specialized skills available. Read a skill's file in full before tackling a challenge it "
                "applies to:")
GENERAL_HEADING = "## General strategies"      # build_system_prompt's heading
DOMAIN_HEADING = "## Available Skills"         # build_system_prompt's heading, lazy-load

PROPOSE_FAIL = """\
The evaluation result for this task:

{eval_result}

## Judge Assessment
{judge_verdict}

## Trajectory signals
{trajectory_signals}

## Compressed trajectory
{compressed_trajectory}

{existing_skills_section}

## Step 1: Analyze the result
Consider the judge assessment, test output, trajectory signals (errors, loops, timeouts), and compressed trajectory.
For EACH distinct issue or failure reason, output:
ISSUE: <one-line summary of what went wrong or what was needed>
DETAIL: <specific commands, files, or techniques that were missing>

## Step 2: Propose a GENERALIZABLE skill
Extract a skill that helps on a BROAD CLASS of similar tasks, not just this one task.

TOPIC: <kebab-case BROAD domain>
ACTION: NEW / ENHANCE / NONE
TARGET: existing_skill_name (only for ENHANCE)
NAME: short-kebab-name describing the TECHNIQUE CLASS, not a specific tool or task
DESCRIPTION: one sentence saying WHEN this skill applies — the agent sees ONLY this line to decide whether to read the skill. Must be broad enough to cover multiple tasks but specific enough to be useful.
CONTENT:
## When to use
- (what class of tasks this applies to)
## Key techniques
- (specific commands, tools, or approaches that work across tasks in this domain)
## Common pitfalls
- (failure modes that recur in this domain, with solutions)

Rules:
- Bullet points, not paragraphs. CONTENT must be under 200 words.
- Be SPECIFIC: include actual commands, flags, patterns — but generalize them (use placeholders like <model>, <arch>, <input_file>)
- The skill must be useful for UNSEEN future tasks, not just a replay of this task
- Do NOT include task-specific details (specific filenames, URLs, dataset names, model names)
- CONTENT should cover the TECHNIQUE CLASS, not one instance
- Prefer ENHANCE over NEW if an existing skill covers the same domain
- TOPIC should match an existing topic if applicable; create a new one only if no existing topic fits
- If the failure was purely task-specific with no generalizable lesson, output ACTION: NONE"""

CURATOR = """\
You are a skill curator for a task-solving agent. You review skill proposals \
and decide which to keep in the skill library for topic: {topic}.

Your goal: build a library of BROADLY REUSABLE skills, not task-specific walkthroughs.

## Current Skill Library ({n_skills}/{max_skills} slots used):
{existing_skills_list}

## Proposals from this batch:
{proposals_list}

For each proposal, output ONE of:

ACCEPT: <proposal_name>

MERGE: <proposal_name> INTO <existing_skill_name>
NEW_CONTENT:
(merged content, under 200 words, bullet points only)

SKIP: <proposal_name>
REASON: <brief reason>

Rules:
- SKIP proposals that are purely task-specific walkthroughs with no reusable insight
- Overlaps existing → MERGE (preferred). When merging, keep specific commands and techniques
- Budget full → can only MERGE existing, or SKIP
- Keep skills SHORT and SPECIFIC — actual commands, flags, and techniques
- Prefer MERGE over SKIP when possible

If no proposals: NO_PROPOSALS"""

GENERAL_CURATOR = """\
You are a meta-learning curator. You analyze patterns ACROSS tasks \
to distill general TECHNIQUES that help the agent on ANY task.

Your goal: extract broadly applicable problem-solving strategies and tool-usage patterns, \
NOT summaries of individual task solutions.

## {analysis_heading} ({n_tasks} tasks):
{task_summaries}

## Current General Skills ({n_general}/{max_general} slots):
{general_skills_list}

For REPEATED patterns across 2+ different tasks, output:

NEW_GENERAL: <kebab-name describing the TECHNIQUE, e.g. "systematic-binary-analysis" not "solve-reverse-engineering">
DESCRIPTION: <one line saying WHEN this skill applies — the agent sees ONLY this line to decide whether to read the skill. Be specific: "For tasks requiring systematic analysis of compiled binaries" not "For security tasks">
CONTENT:
## When to use
- (what class of tasks or situations this applies to)
## Strategy
- (3-5 bullet points: specific commands, flags, pipelines — reusable across tasks)
## Common mistakes
- (1-2 pitfalls observed across tasks, with fixes)
(Under 200 words, bullet points only)

UPDATE_GENERAL: <existing-name>
NEW_CONTENT:
(updated content, under 200 words — generalize, don't append task-specific details)

DELETE_GENERAL: <existing-name>
REASON: <why — e.g. too task-specific, subsumed by another skill, contains forbidden content>

If no cross-task patterns: NO_PATTERNS

Rules:
- Max {max_general} general skills. Quality > quantity.
- Must appear in 2+ different tasks to be general.
- SPECIFIC and ACTIONABLE — actual commands and techniques, not generic advice
- Prefer UPDATE over NEW.
- DELETE skills that are too narrow or no longer useful."""
