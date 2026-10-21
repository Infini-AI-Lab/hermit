# The prompt text below is derived from google-research/harness2 core/prompts.py (_SURFACE_OPENCODE, _INV_TEMPLATE and
# _DOM_TEMPLATE), Copyright 2026 Google LLC, Apache-2.0.
"""The `harness2` template's editable surface, as upstream Harness² sees it.

Upstream edits a tree with the paper's component paths; the template keeps the four always-on files under harness/:

    systemprompt.md                       <-> harness/systemprompt.md      c1  always on
    guardrails.md                         <-> harness/guardrails.md        c2  always on
    LongTermMEMORY.md                     <-> harness/LongTermMEMORY.md    c3  always on
    tool_descriptions/tool_guidance.md    <-> harness/tool_guidance.md     c4  always on
    skills/<name>/SKILL.md                <-> skills/<name>/SKILL.md       c6  rendered inline into ADAPTER.md

Upstream's stage prompts describe the surface per executor and know only OpenCode's and Codex's, so `register()` adds
this one: the OpenCode text with what this executor cannot use taken out (on-demand skills, scripts, subagents,
plugins, Jinja rendering), the way upstream derives its Codex text. The rest of every prompt is upstream's, verbatim.
"""
from __future__ import annotations

from pathlib import Path
import shutil

from harness2.core import prompts as P
from harness2.core import tree as T

from hermit.algorithms.evolvers.harness2.core import surface as S

NAME = "mha_prompt"
TO_TEMPLATE = {"systemprompt.md": "harness/systemprompt.md", "guardrails.md": "harness/guardrails.md",
               "LongTermMEMORY.md": "harness/LongTermMEMORY.md",
               "tool_descriptions/tool_guidance.md": "harness/tool_guidance.md"}
FROM_TEMPLATE = {v: k for k, v in TO_TEMPLATE.items()}
TEMPLATE_FILES = ("adapter.yaml", "harness2.yaml", "check.py", "EVOLVER.md", "README.md", "skills/README.md")

SURFACE = T.SurfaceSpec(
    name=NAME,
    always_on=tuple(TO_TEMPLATE),
    always_on_max=24_000,
    always_on_cap_reason="every always-on character is pasted into the agent's prompt on every model call",
    skill_dir="skills",
    skill_name_rule="match_if_present",
    trigger_hint=('Fix it by adding a trigger line naming `{name}` to systemprompt.md or guardrails.md, e.g. '
                  '"When <the situation this skill handles>, follow the `{name}` skill."'),
    autotrigger_file="guardrails.md",
    trigger_line="- When {desc}, follow the `{name}` skill.\n",
    subagent_requires_mode=False,
    plugin_dir=None,
    hooks_file=None,
    stubs=(("skills/README.md", "<!-- c6: a reusable workflow as skills/<name>/SKILL.md with frontmatter `description:`; "
                                "its body is pasted into the agent's prompt after the always-on files. -->\n"),),
)

SURFACE_MD = """\
# WHAT YOU CAN CHANGE

The harness is a tree of components. On this agent every one of them is pasted into the agent's system prompt, on
every model call:

| path | what it is | when it is in context |
|---|---|---|
| `systemprompt.md` | role and procedure | ALWAYS ON -- every token competes with the agent's own output budget |
| `guardrails.md` | standing rules | ALWAYS ON -- same cost |
| `LongTermMEMORY.md` | durable domain facts | ALWAYS ON -- same cost |
| `tool_descriptions/tool_guidance.md` | how to use the tools | ALWAYS ON -- same cost |
| `skills/<name>/SKILL.md` | a reusable workflow | ALSO ALWAYS ON here: its body follows the always-on files |

This agent has no shell, no skill loader, no subagents and no plugin hooks: its only tools are the ones its
trajectories show. `scripts/`, `sub_agents/` and `plugin/` do not exist on this surface -- state a procedure in
words, in the component where it belongs.

`skills/` -- frontmatter needs a `description:`; a `name:` line, if present, must match the directory. A skill is a
named, self-contained procedure for a recognisable situation; keeping it separate from the always-on files lets a
later task in this area find it and copy it.

Change only what a trajectory shows needs changing -- but WHERE you change something, the FORM matters as much as the
content.

EVERY WORD IS PAID FOR ON EVERY CALL. The harness is in context on every model call, competing with the agent's own
output budget -- and the most costly failure on this work is a run that reasons until the budget is gone and produces
nothing. Adding prose makes that MORE likely, not less. If you add text, cut something else to pay for it.

So prefer, in this order:
  - a SKILL for anything with a recognisable trigger -- a procedure for a document type, a format, a recurring
    situation.
  - ALWAYS-ON TEXT only for a short rule that genuinely applies to every run.

THREE RULES THAT DISCARD THE WHOLE PROPOSAL. Validation runs before any rollout, and a harness that fails it is thrown
away entire.

1. EVERY SKILL NEEDS A TRIGGER LINE -- the most violated rule here by a wide margin. If you create or keep
   `skills/<name>/SKILL.md`, its directory name must appear VERBATIM in `systemprompt.md` or `guardrails.md`, in the
   SAME edit:

       When <the situation this skill handles>, follow the `<name>` skill.

2. SIZE. The four always-on files together hold at most 24,000 characters; each file, a SKILL.md included, at most
   24,000 bytes; everything together, as pasted into the prompt, at most 40,000 bytes.

3. NOTHING OFF THIS SURFACE. A script, a subagent, a plugin, or a file anywhere else is rejected, not ignored.

THE DELIVERABLE'S SHAPE BELONGS TO THE REQUEST, NOT TO YOU. A harness changes HOW the agent works; it must never
change WHAT SHAPE it delivers. Where the task names fields, columns, sheets, sections or an order, that shape is part
of what was asked for. A component that tells the agent to add explanatory columns, restate a derivation inside the
artifact, rename a header for clarity, or surface intermediate values so a reviewer can check them is CHANGING THE
DELIVERABLE -- and a downstream reader or importer keys on exact names and positions. Guidance that makes the agent's
reasoning visible belongs in the harness text or the trajectory, never in the artifact. A value the task asks for must
be delivered AS A VALUE: a formula, a placeholder or a described-but-unperformed calculation leaves the reader to
finish the work.

# HOW TO MAKE A CHANGE

The harness is in `context/harness/`, and it is YOURS TO EDIT. Open the files, change them in place with your normal
tools, and create new ones where a new component is the right lever. Your edits on disk ARE the proposal -- there is
no separate format to reproduce them in, and nothing you write in this reply changes the harness.

- Files you do not touch are inherited unchanged. Do not rewrite a file to keep it the same.
- Delete a file to remove that component.
- If the evidence does not argue for any change, leave everything as it is and say KEEP_BASE in your notes. That is a
  real answer, not a failure: adaptation the evidence does not support costs context and buys nothing.

Then reply with ONE `<notes>` block and nothing else: for each change, the moment in a trajectory that motivated it.
The notes are read by a person, not applied by a machine."""

# Upstream's INV and DOM dimensions with the levers this executor lacks taken out; each replaced passage must exist.
_INV_EDITS = (
    ("- MECHANICAL REPETITION: use `scripts/<name>.py` for repeated deterministic extraction,\n"
     "  conversion or checking, with clear inputs, outputs and failure behavior.",
     "- MECHANICAL REPETITION: write the exact repeated procedure -- its inputs, its steps and the\n"
     "  check of its result -- as a skill, step by step."),
    ("use an on-demand skill for a narrow multi-step procedure", "use a skill for a narrow multi-step procedure"),
    (", and carry any helper it requires.", "."),
    ("__LOOP_LEVER__", "rule in `guardrails.md` -- for example, never ending a run without the {deliverable}"),
)
_DOM_EDITS = (
    ("It costs\n  nothing until that situation arises, and the next task in this {domain} can invoke it.",
     "On this\n  agent its body is in context on every call, so keep it short; the next task in this {domain} can "
     "copy it."),
    ("__TRIGGER_SENTENCE__", "A skill exists only once its name appears in `systemprompt.md` or `guardrails.md`: "
                             "write that trigger line in the same edit."),
)


def _edit(text: str, edits) -> str:
    for old, new in edits:
        if old not in text:
            raise RuntimeError(f"upstream's prompt text changed; cannot find {old[:60]!r}")
        text = text.replace(old, new)
    return text


def register() -> None:
    """Make upstream's stage prompts know this surface (idempotent)."""
    if NAME in P._SURFACE_TEXTS:
        return
    tts = "\n\n".join([P._surface_part(SURFACE_MD, "THREE RULES THAT DISCARD", "THE DELIVERABLE'S SHAPE"),
                       P._surface_part(SURFACE_MD, "# HOW TO MAKE A CHANGE")])
    P._SURFACE_TEXTS[NAME] = P._SurfaceText(surface_md=SURFACE_MD, inv=_edit(P._INV_TEMPLATE, _INV_EDITS),
                                            dom=_edit(P._DOM_TEMPLATE, _DOM_EDITS), tts_surface=tts)


def from_adapter(adapter: Path) -> T.Tree:
    """An adapter's component tree, with upstream's paths."""
    tree = S.read_tree(S.load_surface(adapter), adapter)
    return {FROM_TEMPLATE.get(rel, rel): body for rel, body in tree.items()}


def to_adapter(tree: T.Tree, template: Path, dest: Path) -> Path:
    """An adapter in the template's layout holding `tree` (upstream's paths), with ADAPTER.md rendered from it. The
    template's own files are copied from `template` (an adapter of the same template)."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for rel in TEMPLATE_FILES:
        if (template / rel).is_file():
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(template / rel, dest / rel)
    S.write_tree(S.load_surface(dest), {TO_TEMPLATE.get(rel, rel): body for rel, body in tree.items()}, dest,
                 render=True)
    return dest


def off_surface(tree: T.Tree) -> list[str]:
    """Paths upstream accepts but this executor cannot use (scripts, subagents, plugins)."""
    return sorted(rel for rel in tree if rel not in TO_TEMPLATE and not T._rx(SURFACE)["skill"].fullmatch(rel))
