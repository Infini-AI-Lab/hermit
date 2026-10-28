"""Template check for `harness2-codex` adapters: the tree is a valid Codex harness by upstream Harness²'s rules
(google-research/harness2, `harness2.core.tree.validate` on `CodexSubstrate.CODEX_SURFACE`), nothing lies off that
surface, no file carries a grader's wording (upstream's tripwire), and ADAPTER.md is AGENTS.md (the framework's
context export is the always-on text; AGENTS.md is the file Codex reads).

The rules are restated here rather than imported: the framework runs a template check in a sandbox that shows only
its own dependencies, and `harness2` is an optional package (the harness2 dependency group in pyproject.toml). The Evolver's proposals also pass upstream's own
validator before they are probed, so this check is the framework's gate, not the only one.

Upstream's Codex surface:
    AGENTS.md                              the one always-on file: at most 8,000 characters, never empty
    .agents/skills/<name>/SKILL.md         frontmatter `description:` required; `name:` optional, must match the dir
    .agents/skills/<name>/scripts/<file>   a helper carried with its skill; a .py must compile
    scripts/<name>.py                      a deterministic helper; must compile
    sub_agents/<name>/AGENT.md             frontmatter `description:` required
    .codex/hooks.json                      {"hooks": {"<Event>": [{"matcher": ..., "hooks": [{"type": "command", ...}]}]}}
Every skill's directory name must appear in AGENTS.md (the trigger line), and a script a skill or AGENTS.md invokes
must be in the tree. The on-demand directories' README.md placeholders are not part of the tree.
"""
from __future__ import annotations

import json
from pathlib import Path
import re

ALWAYS_ON = "AGENTS.md"
ALWAYS_ON_MAX = 8_000
HOOKS_FILE = ".codex/hooks.json"
HOOK_EVENTS = frozenset({"PreToolUse", "PostToolUse", "PermissionRequest", "PreCompact", "PostCompact", "SessionStart",
                         "SubagentStart", "SubagentStop", "UserPromptSubmit", "Stop"})
RESERVED_SCRIPTS = frozenset({"read_doc.py", "pack.py", "unpack.py"})       # upstream's own helpers a skill may name
GRADER_VERDICT_MARKERS = ("deterministic_eval_summary", "deterministic_eval_result", "judge_scope_and_deterministic_status",
                          "Judge findings on the rendered result", '"passed_count"', '"pass_rate"', '"test_pass_rate"')
# The framework's and the template's own files, and upstream's placeholder READMEs: not part of the harness tree.
OWN = frozenset({"ADAPTER.md", "adapter.yaml", "CARD.md", "check.py", "EVOLVER.md", "README.md"})
STUBS = frozenset({"scripts/README.md", "sub_agents/README.md", ".codex/README.md", ".agents/skills/README.md"})
NAME = r"[a-z0-9][a-z0-9_\-]*"
RX = {"skill": re.compile(rf"\.agents/skills/({NAME})/SKILL\.md"),
      "skill_script": re.compile(rf"\.agents/skills/{NAME}/scripts/[A-Za-z0-9_.\-]+"),
      "subagent": re.compile(rf"sub_agents/({NAME})/AGENT\.md"),
      "script": re.compile(r"scripts/[a-z0-9_][a-z0-9_]*\.py"),
      "script_ref": re.compile(r"(?:python3?\s+)((?:\.agents/skills/[\w\-]+/)?scripts/[\w.\-]+\.py)")}


def allowed(rel: str) -> bool:
    return rel == ALWAYS_ON or rel == HOOKS_FILE or any(RX[k].fullmatch(rel) for k in ("skill", "skill_script",
                                                                                          "subagent", "script"))


def read_tree(adapter: Path) -> tuple[dict[str, str], list[str]]:
    """rel -> text for every file on the surface, and the files that are off it."""
    tree, off = {}, []
    for p in sorted(adapter.rglob("*")):
        if not p.is_file() or "__pycache__" in p.parts or ".git" in p.parts or p.suffix == ".pyc":
            continue
        rel = p.relative_to(adapter).as_posix()
        if rel in OWN or rel in STUBS:
            continue
        if allowed(rel):
            tree[rel] = p.read_text(encoding="utf-8", errors="replace")
        else:
            off.append(rel)
    return tree, off


def problems(tree: dict[str, str]) -> list[str]:
    """Upstream's validate(), for this surface, every problem in actionable prose."""
    errs: list[str] = []
    body = tree.get(ALWAYS_ON, "")
    if len(body) > ALWAYS_ON_MAX:
        errs.append(f"always-on content is {len(body)} chars, over the {ALWAYS_ON_MAX} cap; AGENTS.md is injected whole "
                    f"into EVERY turn, and codex additionally truncates it silently at 32,768 bytes")
    if not body.strip():
        errs.append("AGENTS.md is empty — it is the only always-on file on this framework, so an empty one is a harness "
                    "that does not exist")
    for rel in sorted(tree):
        m = RX["skill"].fullmatch(rel)
        if not m:
            continue
        name, fm = m.group(1), tree[rel][:600]
        if not re.search(r"^description:\s*\S", fm, re.M):
            errs.append(f"{rel} needs a `description:` in its frontmatter, or the skill is dropped SILENTLY at load")
        m_name = re.search(r"^name:\s*(\S+)\s*$", fm, re.M)
        if m_name and m_name.group(1) != name:
            errs.append(f"{rel} frontmatter `name: {m_name.group(1)}` OVERRIDES the directory name at load, so every "
                        f"trigger line naming `{name}` stops matching; drop the `name:` line or make it `name: {name}`")
        if name not in body:
            errs.append(f"skill '{name}' is never named in the always-on text, so the agent will not know to invoke it "
                        f"— an untriggered skill is dead weight. Fix it by adding a trigger line naming `{name}` to "
                        f"AGENTS.md, e.g. \"When <the situation this skill handles>, read .agents/skills/{name}/SKILL.md "
                        f"and follow it.\"")
    for rel in sorted(tree):
        m = RX["subagent"].fullmatch(rel)
        if m and not re.search(r"^description:\s*\S", tree[rel][:600], re.M):
            errs.append(f"{rel} needs a `description:`")
    for rel, src in tree.items():
        if rel.endswith(".py") and (RX["script"].fullmatch(rel) or RX["skill_script"].fullmatch(rel)):
            try:
                compile(src, rel, "exec")
            except (SyntaxError, ValueError) as exc:
                detail = (f"{type(exc).__name__}: {exc.msg} (line {exc.lineno})" if isinstance(exc, SyntaxError)
                          else f"{type(exc).__name__}: {exc}")
                errs.append(f"{rel} does not compile: {detail[:160]}")
    if HOOKS_FILE in tree:
        errs += hook_problems(tree[HOOKS_FILE])
    have = {rel for rel in tree if RX["script"].fullmatch(rel) or RX["skill_script"].fullmatch(rel)}
    for rel, src in tree.items():
        if not (RX["skill"].fullmatch(rel) or rel == ALWAYS_ON):
            continue
        for ref in RX["script_ref"].findall(src):
            if ref.rsplit("/", 1)[-1] in RESERVED_SCRIPTS:
                continue
            if ref not in have:
                errs.append(f"{rel} invokes `{ref}` but that file is not in the harness")
    return errs


def hook_problems(src: str) -> list[str]:
    """The hook schema the runtime dispatches; a malformed file never fires rather than erroring."""
    try:
        doc = json.loads(src)
    except json.JSONDecodeError as exc:
        return [f"{HOOKS_FILE} is not valid JSON: {exc}"]
    hooks = doc.get("hooks") if isinstance(doc, dict) else None
    if not isinstance(hooks, dict) or not hooks:
        return [f'{HOOKS_FILE} needs a top-level {{"hooks": {{"<Event>": [...]}}}} object']
    errs = []
    for event, entries in hooks.items():
        if event not in HOOK_EVENTS:
            errs.append(f"{HOOKS_FILE}: unknown event {event!r}; the framework dispatches "
                        f"{', '.join(sorted(HOOK_EVENTS))} (PascalCase)")
            continue
        if not isinstance(entries, list):
            errs.append(f"{HOOKS_FILE}: {event} must map to a LIST of matcher entries")
            continue
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
                errs.append(f"{HOOKS_FILE}: {event}[{i}] needs a `hooks` list")
                continue
            for h in entry["hooks"]:
                if not isinstance(h, dict) or h.get("type") != "command":
                    errs.append(f'{HOOKS_FILE}: {event}[{i}] hook type must be "command" — async/prompt/agent hooks '
                                f"are skipped as 'not supported yet'")
                elif not str(h.get("command", "")).strip():
                    errs.append(f"{HOOKS_FILE}: {event}[{i}] has an empty `command`")
    return errs


def verdict_problems(tree: dict[str, str]) -> list[tuple[str, str]]:
    """Upstream's tripwire: a file whose name or text carries a grader's wording could never be served as evidence."""
    out = []
    for rel in sorted(tree):
        for m in GRADER_VERDICT_MARKERS:
            if m in rel:
                out.append((rel, f"harness file {rel!r} has grader-verdict marker {m!r} in its NAME; rename it"))
            elif m in tree[rel]:
                out.append((rel, f"harness file {rel!r} contains grader-verdict marker {m!r}; a harness that carries "
                                 f"grader output cannot be served as evidence to later tasks"))
    return out


def check(adapter_path: str) -> list[dict]:
    adapter = Path(adapter_path).resolve()
    if not (adapter / ALWAYS_ON).is_file():
        # Never pass an adapter this check cannot see (under bubblewrap, /tmp is an empty tmpfs).
        return [{"path": ALWAYS_ON, "problem": f"no harness2-codex adapter at {adapter} (AGENTS.md missing or not "
                                               f"visible)"}]
    tree, off = read_tree(adapter)
    issues = [{"path": rel, "problem": f"{rel} is off the Codex harness surface (AGENTS.md, .agents/skills/<name>/"
                                       f"SKILL.md and its scripts/, scripts/<name>.py, sub_agents/<name>/AGENT.md, "
                                       f".codex/hooks.json)"} for rel in off]
    issues += [{"path": "", "problem": p} for p in problems(tree)]
    issues += [{"path": rel, "problem": p} for rel, p in verdict_problems(tree)]
    context = (adapter / "ADAPTER.md").read_text(encoding="utf-8") if (adapter / "ADAPTER.md").is_file() else ""
    if context != tree.get(ALWAYS_ON, ""):
        issues.append({"path": "ADAPTER.md", "problem": "ADAPTER.md is not AGENTS.md: it is the always-on text as the "
                       "framework exports it, so edit AGENTS.md and copy it"})
    return issues
