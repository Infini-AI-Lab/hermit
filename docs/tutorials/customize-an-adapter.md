# Customize an adapter

You want the agent to start from your own instructions, skills or tools, or to let the learner change more (or
less) than a built-in template allows. An adapter's shape is its template; customizing means copying a template,
editing the copy, and pointing a run at it. No model call is needed to see the effect: the walkthrough below runs
offline with the echo task of tutorial 3.

## Copy a template

```bash
mkdir -p .demo
cp -R hermit/algorithms/templates/prompt_only .demo/my_template
```

`.demo/my_template/` holds `.hermit/adapter.yaml` (the template's metadata), `.hermit/EVOLVER.md` (how a learner
should edit), `exports.yaml` (what the agent receives), `ADAPTER.md` (the instructions) and `README.md`. A learner
never changes `.hermit/`; you, as the template author, do.

## Change the starting instructions

Edit `ADAPTER.md`. This needs no metadata change: the learner starts from your text.

```bash
cat > .demo/my_template/ADAPTER.md <<'EOF'
# Working rules

Repeat the input exactly. Do not add punctuation or commentary.
EOF
```

## Add a skill, and let the learner edit skills

Add a skill file, declare the `skills/` export in `exports.yaml`, and add the learnable path to
`.hermit/adapter.yaml`:

```bash
mkdir -p .demo/my_template/skills/check-input
cat > .demo/my_template/skills/check-input/SKILL.md <<'EOF'
---
name: check-input
description: Read the input twice before answering.
---
Read the whole input, then read it again, then answer.
EOF
```

In `.demo/my_template/exports.yaml`:

```yaml
context: ADAPTER.md
skills: skills
```

In `.demo/my_template/.hermit/adapter.yaml`:

```yaml
template:
  id: my-template            # a new id: this is not prompt-only any more
  version: "1.0"
  contract: "2"
  name: My template
structure:
  learnable:
    - exports.yaml
    - path: ADAPTER.md
      format: {text: true, max_bytes: 40000}
    - path: skills/**
      format: {text: true, max_bytes: 4000, max_files: 8}
  fixed:
    - README.md
```

`learnable` is what the learner may change, with limits; `fixed` is what must stay as shipped. Every file outside
`.hermit/` must be one or the other. Here `exports.yaml` is learnable (prompt-only fixes it); list it under `fixed`
instead to keep the exports as you wrote them. The learner reads `.hermit/EVOLVER.md` to know how to edit: say there
that skills exist and what one looks like.

## Add a tool

A tool is Python inside the adapter, declared in `exports.yaml`; the host sees it as an MCP tool.

```bash
cat > .demo/my_template/tools.py <<'EOF'
def count_words(text: str) -> dict:
    return {"count": len(text.split())}
EOF
```

```yaml
context: ADAPTER.md
skills: skills
tools:
  - name: count_words
    description: Count whitespace-separated words in text.
    entrypoint: tools.py:count_words
    input_schema: {type: object, properties: {text: {type: string}}, required: [text], additionalProperties: false}
    output_schema: {type: object, properties: {count: {type: integer}}, required: [count], additionalProperties: false}
```

and `tools.py` under `fixed` in `.hermit/adapter.yaml`, so the learner cannot change it. `docs/_examples/file_adapter/` is a complete
adapter with a skill, a role and this tool.

## Add a checker

A rule of your own that every candidate must pass, run after the built-in checks. It lives in `.hermit/`, which
the learner cannot change:

```bash
cat > .demo/my_template/.hermit/check.py <<'EOF'
from pathlib import Path

def check(adapter_path) -> list:
    text = (Path(adapter_path) / "ADAPTER.md").read_text()
    return [] if "Repeat the input" in text else [{"path": "ADAPTER.md", "problem": "the rule about repeating the input is gone"}]
EOF
```

and in `.hermit/adapter.yaml`:

```yaml
check: .hermit/check.py:check
```

The path is relative to the adapter root; the checker is always loaded from the template root.

## Run it

Point a run at the directory. With the offline echo task and its recording Evolver:

```bash
hermit check --config docs/_examples/interface_task/run.yaml template=../../../.demo/my_template trainer.run_name=my-template
hermit train --config docs/_examples/interface_task/run.yaml template=../../../.demo/my_template trainer.run_name=my-template
hermit deploy --adapter .demo/my_template --workspace .demo/ws --host codex && cat .demo/ws/AGENTS.md
```

Paths in overrides are relative to the config file. `check` loads your template and reports what does not fit
(a file that is neither learnable nor fixed, a schema error, a checker that fails on the template itself). The
train run's tree is created under the template's new id, so it never mixes with prompt-only's.

Two rules to keep in mind: `.hermit/` is the template's and every trained adapter carries it unchanged, so change
it in the template before training, not in a trained adapter; and if `.hermit/adapter.yaml` or `exports.yaml`
changes, give the copy its own `template.id` or a new `version`. The fields are in the
[adapter metadata reference](../framework/adapter-manifest.md).

Next: [Write your own Evolver](custom-evolver.md).
