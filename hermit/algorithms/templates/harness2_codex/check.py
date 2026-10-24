"""Template check for `harness2-codex` adapters: the tree is a valid Codex harness by upstream Harness²'s own rules
(harness2.substrate.codex.CODEX_SURFACE: a trigger line in AGENTS.md for every skill, scripts that compile, a helper
a skill invokes travels with it, hooks.json in the hook schema, AGENTS.md non-empty and under its cap), nothing lies
off that surface, no file carries a grader's wording, and ADAPTER.md is AGENTS.md (the framework's context export
is the always-on text; AGENTS.md is the file Codex reads)."""
from __future__ import annotations

from pathlib import Path

# The framework's and the template's own files: not part of the harness tree.
OWN = frozenset({"ADAPTER.md", "adapter.yaml", "CARD.md", "check.py", "EVOLVER.md", "README.md"})


def check(adapter_path: str) -> list[dict]:
    from harness2.core import tree as T
    from harness2.core.context import assert_tree_has_no_grader_verdicts
    from harness2.substrate.codex import CODEX_SURFACE
    adapter = Path(adapter_path).resolve()
    if not (adapter / "AGENTS.md").is_file():
        # Never pass an adapter this check cannot see (under bubblewrap, /tmp is an empty tmpfs).
        return [{"path": "AGENTS.md", "problem": f"no harness2-codex adapter at {adapter} (AGENTS.md missing or "
                                                 f"not visible)"}]
    rejected: list[str] = []
    tree = T.read_tree(CODEX_SURFACE, adapter, rejected=rejected)
    issues = [{"path": rel, "problem": f"{rel} is off the Codex harness surface (AGENTS.md, .agents/skills/<name>/"
                                       f"SKILL.md and its scripts/, scripts/<name>.py, sub_agents/<name>/AGENT.md, "
                                       f".codex/hooks.json)"}
              for rel in rejected if rel not in OWN and not rel.startswith(".git/") and not rel.endswith(".pyc")]
    try:
        T.validate(CODEX_SURFACE, tree, allow_subagents=True)
    except ValueError as exc:
        issues += [{"path": "", "problem": problem} for problem in str(exc).split(" | ")]
    for rel in sorted(tree):
        try:   # a file carrying a grader-verdict marker could never be served as evidence to the Evolver
            assert_tree_has_no_grader_verdicts({rel: tree[rel]})
        except ValueError as exc:
            issues.append({"path": rel, "problem": str(exc)})
    agents = (adapter / "AGENTS.md").read_text(encoding="utf-8")
    context = (adapter / "ADAPTER.md").read_text(encoding="utf-8") if (adapter / "ADAPTER.md").is_file() else ""
    if context != agents:
        issues.append({"path": "ADAPTER.md", "problem": "ADAPTER.md is not AGENTS.md: it is the always-on text as "
                       "the framework exports it, so edit AGENTS.md and copy it"})
    return issues
