# 4. Customize an adapter

You want the agent to start from your own instructions, skills or tools, or to let the learner change more (or
less) than a built-in template allows. An adapter's shape is its template; customizing means copying a template,
editing the copy, and pointing a run at it. No model call is needed to see the effect: the walkthrough below runs
offline with the echo task of tutorial 3.

## Copy a template

```bash
mkdir -p .demo
cp -R harnessadapter/templates/prompt_only .demo/my_template
```

`.demo/my_template/` holds `adapter.yaml` (the manifest), `ADAPTER.md` (the instructions), `EVOLVER.md` (how a
learner should edit) and `README.md`.

## Change the starting instructions

Edit `ADAPTER.md`. This needs no manifest change: the learner starts from your text.

```bash
cat > .demo/my_template/ADAPTER.md <<'EOF'
# Working rules

Repeat the input exactly. Do not add punctuation or commentary.
EOF
```

## Add a skill, and let the learner edit skills

Add a skill file and declare the `skills/` export and the learnable path:

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

In `.demo/my_template/adapter.yaml`:

```yaml
template:
  id: my-template            # a new id: this is not prompt-only any more
  version: "1.0"
  contract: "1"
  name: My template
exports:
  context: ADAPTER.md
  skills: skills
structure:
  learnable:
    - path: ADAPTER.md
      format: {text: true, max_bytes: 40000}
    - path: skills/**
      format: {text: true, max_bytes: 4000, max_files: 8}
  fixed:
    - EVOLVER.md
    - README.md
```

`learnable` is what the learner may change, with limits; `fixed` is what must stay as shipped. Every file in the
adapter must be one or the other. The learner reads `EVOLVER.md` to know how to edit: say there that skills exist
and what one looks like.

## Add a tool

A tool is Python inside the adapter, declared in the manifest; the host sees it as an MCP tool.

```bash
cat > .demo/my_template/tools.py <<'EOF'
def count_words(text: str) -> dict:
    return {"count": len(text.split())}
EOF
```

```yaml
exports:
  context: ADAPTER.md
  skills: skills
  tools:
    - name: count_words
      description: Count whitespace-separated words in text.
      entrypoint: tools.py:count_words
      input_schema: {type: object, properties: {text: {type: string}}, required: [text], additionalProperties: false}
      output_schema: {type: object, properties: {count: {type: integer}}, required: [count], additionalProperties: false}
```

and `tools.py` under `fixed`, so the learner cannot change it. `docs/_examples/file_adapter/` is a complete
adapter with a skill, a role and this tool.

## Add a checker

A rule of your own that every candidate must pass, run after the built-in checks:

```bash
cat > .demo/my_template/check.py <<'EOF'
def check(adapter_path) -> list:
    text = (adapter_path / "ADAPTER.md").read_text()
    return [] if "Repeat the input" in text else [{"path": "ADAPTER.md", "problem": "the rule about repeating the input is gone"}]
EOF
```

```yaml
check: check.py:check
```

with `check.py` under `fixed`.

## Run it

Point a run at the directory. With the offline echo task and its recording Evolver:

```bash
harnessadapter check --config docs/_examples/interface_task/run.yaml template=../../../.demo/my_template trainer.run_name=my-template
harnessadapter train --config docs/_examples/interface_task/run.yaml template=../../../.demo/my_template trainer.run_name=my-template
harnessadapter deploy --adapter .demo/my_template --output .demo/ws --host codex && cat .demo/ws/AGENTS.md
```

Paths in overrides are relative to the config file. `check` loads your template and reports what does not fit
(a file that is neither learnable nor fixed, a schema error, a checker that fails on the template itself). The
train run's tree is created under the template's new id, so it never mixes with prompt-only's.

Two rules to keep in mind: the manifest's first five sections are the template's identity, fixed across every
adapter trained from it, so change them in the template before training, not in a trained adapter; and if the
manifest changes, give the copy its own `template.id` or a new `version`. The fields are in the
[adapter.yaml reference](../framework/adapter-manifest.md).

Next: [Write your own Evolver](custom-evolver.md).
