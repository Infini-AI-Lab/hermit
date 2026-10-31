"""The editable surface of a Harness² adapter, as upstream Harness² sees it.

The `harness2-codex` template is upstream's own Codex surface (harness2.substrate.codex.CODEX_SURFACE), and the
adapter is the Codex tree itself (AGENTS.md, .agents/skills/, scripts/, sub_agents/, .codex/hooks.json), so no path
changes hands and upstream's stage prompts already describe the surface. ADAPTER.md, the framework's context export,
is kept equal to AGENTS.md.
"""
from __future__ import annotations

from pathlib import Path
import shutil

from harness2.core import tree as T


class CodexLayout:
    """The `harness2-codex` template: upstream's Codex tree as it is; ADAPTER.md is a copy of AGENTS.md."""
    template_id = "harness2-codex"
    name = "cx"                      # upstream's substrate name, part of its run namespaces
    allow_subagents = True
    install_mode = "codex_root"
    off_surface_hint = ("AGENTS.md, .agents/skills/<name>/SKILL.md and its scripts/, scripts/<name>.py, "
                        "sub_agents/<name>/AGENT.md, .codex/hooks.json")

    def __init__(self) -> None:
        from harness2.substrate.codex import CODEX_SURFACE
        self.surface = CODEX_SURFACE
        self.template_files = (".hermit", "exports.yaml", "README.md",
                               *(rel for rel, _ in CODEX_SURFACE.stubs))

    @staticmethod
    def register() -> None:
        return None                   # upstream's prompts know the codex surface

    def from_adapter(self, adapter: Path) -> T.Tree:
        """An adapter's component tree, with upstream's paths (the stubs and the framework's own files are not it)."""
        return T.read_tree(self.surface, adapter)

    def to_adapter(self, tree: T.Tree, template: Path, dest: Path) -> Path:
        """An adapter holding `tree`, with the template's own files copied from `template` (an adapter of the same
        template) and ADAPTER.md equal to AGENTS.md."""
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        for rel in self.template_files:
            if (template / rel).is_dir():
                shutil.copytree(template / rel, dest / rel)
            elif (template / rel).is_file():
                (dest / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(template / rel, dest / rel)
        T.write_tree(self.surface, tree, dest, stubs=True)
        (dest / "ADAPTER.md").write_text(tree.get("AGENTS.md", ""), encoding="utf-8")
        return dest

    @staticmethod
    def off_surface(tree: T.Tree) -> list[str]:
        """Paths upstream accepts but this executor cannot use: none, the executor is Codex."""
        return []

    @staticmethod
    def upstream_path(rel: str) -> str:
        """A template path as upstream names it (for a problem the template check reports)."""
        return rel


def layout_of(adapter: Path):
    """The layout of an adapter (or template), by its template id."""
    import yaml
    data = yaml.safe_load((Path(adapter) / ".hermit" / "adapter.yaml").read_text()) or {}
    ident = str((data.get("template") or {}).get("id") or "")
    if ident == CodexLayout.template_id:
        return CodexLayout()
    raise ValueError(f"harness2_upstream trains the harness2-codex template, not {ident!r}")
