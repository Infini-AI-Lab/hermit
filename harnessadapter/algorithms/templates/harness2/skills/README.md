# Skills

One directory per skill: `skills/<name>/SKILL.md`, with YAML frontmatter

    ---
    description: <one line: the situation it handles>
    ---

and the procedure as its body. A skill is used only if one of the always-on files under `harness/` names it
(`When <situation>, follow the `<name>` skill.`). Under this template's `skills: inline`, every skill body is
rendered into ADAPTER.md.
