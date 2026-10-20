"""Template check for `harness2` adapters: the component tree validates against the surface in harness2.yaml, and
ADAPTER.md is exactly its rendering (it is generated: edit the component files, not ADAPTER.md)."""
from __future__ import annotations

from pathlib import Path


def check(adapter_path: str) -> list[dict]:
    from harnessadapter.algorithms.evolvers.harness2.core import surface as S
    adapter = Path(adapter_path).resolve()
    if not (adapter / "harness2.yaml").is_file():
        # Never pass an adapter this check cannot see (under bubblewrap, /tmp is an empty tmpfs).
        return [{"path": "", "problem": f"no harness2 adapter at {adapter} (harness2.yaml missing or not visible)"}]
    spec = S.load_surface(adapter)
    rejected: list[str] = []
    tree = S.read_tree(spec, adapter, rejected=rejected)
    issues = [{"path": rel, "problem": f"{rel} is off the harness2 surface (see harness2.yaml)"} for rel in rejected]
    issues += [{"path": path, "problem": problem} for path, problem in S.problems(spec, tree)]
    from harnessadapter.algorithms.evolvers.harness2.core.context import assert_tree_has_no_grader_verdicts
    for rel in sorted(tree):
        try:   # a file carrying a grader-verdict marker could never be served as evidence to the Evolver
            assert_tree_has_no_grader_verdicts({rel: tree[rel]})
        except ValueError as exc:
            issues.append({"path": rel, "problem": str(exc)})
    rendered = S.render_context(spec, tree)
    current = (adapter / "ADAPTER.md").read_text(encoding="utf-8") if (adapter / "ADAPTER.md").is_file() else ""
    if current != rendered:
        issues.append({"path": "ADAPTER.md", "problem": "ADAPTER.md is not the rendering of harness/*.md and "
                       "skills/: it is generated, so edit the component files and render it again"})
    return issues
