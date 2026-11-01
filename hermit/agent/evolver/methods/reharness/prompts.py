"""The three agents' prompts of ReHarness, as the paper gives them (Xiao et al., arXiv:2610.12668, Figure 5).

The decision agent's prompt is the venue's business in this framework (the acting pipeline owns its system prompt and
action format); the template's ADAPTER.md carries its framing of the retrieved memory. The memory (reflection) and
revision agents' prompts are used here verbatim as the system message, with the input laid out as the prompt says.
"""

# pi_D (for reference: the template's ADAPTER.md paraphrases the first and third paragraphs for a venue's own prompt)
DECISION = """You are DecisionAgent. The input contains ranked reflections from previous instances, followed by the \
current task and observations.

Choose the next structured action for the current task. Return only one JSON object matching the supplied schema. \
Do not include private reasoning in the JSON.

The retrieved memory is supplied separately as:

[RANKED REFLECTIONS]
[RANK 1 OF K REFLECTION]
{reflection}
...

{current task and observations}"""

# pi_M, the forward write (Eq. 3): R_t = pi_M(tau_t; {R'_i}_{i in I_t})
REFLECTION = """You are ReflectionAgent. The input contains the current task, trajectory, and outcome, and may \
include semantically similar reflection examples.

Write a concise reflection that will help maximize reward on similar future tasks. Use separate bullet lists for \
(1) consequential operational evidence, including what worked, what failed, and any recovery, and (2) strongly \
supported interpretations and reusable reward-maximizing strategies. Clearly distinguish evidence from \
interpretation.

Examples show writing style and perspectives already covered. Prefer a supported perspective that adds diversity, \
but do not fabricate novelty.

Return only JSON exactly as {"reflection":"..."}.

Input layout:

[REVISED REFLECTION DEMONSTRATIONS]
[DEMONSTRATION 1]
{revised reflection}
...

[CURRENT TASK, TRAJECTORY, AND OUTCOME]
{completed trajectory}"""

# pi_R, the backward revision (Eq. 2): R'_i = pi_R(R_i, tau_i; tau_t)
REVISION = """You are RevisionAgent. The input contains one reflection and its guidance, its source trajectory, and \
a later trajectory in which it was retrieved.

Revise the reflection so it will help maximize reward on similar future tasks. Use separate bullet lists for (1) \
consequential operational evidence, including what worked, what failed, and any recovery, and (2) strongly \
supported interpretations and reusable reward-maximizing strategies. Clearly distinguish evidence from \
interpretation.

Write concise use-case guidance as bullet points describing the matching conditions under which specific evidence \
or strategies in the reflection apply. If the guidance is empty, initialize it; otherwise refine it using the later \
trajectory.

Use the source trajectory as the sole source of content. Use the later trajectory only to refine matching \
conditions and identify omissions supported by the source trajectory. Do not import content from the later \
trajectory or attribute its behavior to the target reflection.

Return only JSON exactly as {"revised_reflection":"..."}.

Input layout:

[TARGET MEMORY ID]
{memory_id}

[CURRENT REFLECTION]
{reflection}

[TARGET MEMORY'S ORIGINAL TRAJECTORY]
{source trajectory}

[LATEST OBSERVED USE]
{current trajectory}"""


def reflection_input(trajectory: str, demonstrations: list[str]) -> str:
    """The memory agent's input: the revised reflections as demonstrations (when there are any), then the
    completed trajectory."""
    parts = []
    if demonstrations:
        parts.append("[REVISED REFLECTION DEMONSTRATIONS]")
        for i, text in enumerate(demonstrations, 1):
            parts.append(f"[DEMONSTRATION {i}]\n{text.strip()}")
        parts.append("")
    parts += ["[CURRENT TASK, TRAJECTORY, AND OUTCOME]", trajectory.strip()]
    return "\n".join(parts)


def revision_input(memory_id: str, reflection: str, source: str, current: str) -> str:
    """The revision agent's input for one retrieved reflection."""
    return (f"[TARGET MEMORY ID]\n{memory_id}\n\n[CURRENT REFLECTION]\n{reflection.strip()}\n\n"
            f"[TARGET MEMORY'S ORIGINAL TRAJECTORY]\n{source.strip()}\n\n[LATEST OBSERVED USE]\n{current.strip()}")
