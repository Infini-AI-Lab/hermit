"""check_spec and repair on the two built-in templates."""
from pathlib import Path
import shutil

from hermit.adapter.manifest import matches
from hermit.spec import check_spec, repair

TEMPLATES = Path(__file__).resolve().parents[1] / "hermit" / "algorithms" / "templates"


def copy(src, dst):
    shutil.copytree(src, dst)
    return dst


def test_templates_conform():
    for name in ("prompt_only", "lean_collab"):
        result = check_spec(TEMPLATES / name, TEMPLATES / name)
        assert result.passed, result.issues


def test_glob_matching():
    assert matches("skills/a/SKILL.md", "skills/**") and matches("skills/a/SKILL.md", "skills")
    assert matches("ADAPTER.md", "ADAPTER.md") and not matches("x/ADAPTER.md", "ADAPTER.md")
    assert matches("memory/x.md", "memory/*.md") and not matches("memory/a/x.md", "memory/*.md")


def test_violations_are_found_and_repaired(tmp_path):
    root = TEMPLATES / "lean_collab"
    parent = copy(root, tmp_path / "parent")
    adapter = copy(root, tmp_path / "adapter")
    (adapter / "collab.py").write_text("# broken\n")                        # fixed file changed (1) → deploy fails too
    (adapter / "notes.txt").write_text("stray\n")                          # outside learnable paths (2)
    (adapter / "skills" / "big").mkdir()
    (adapter / "skills" / "big" / "SKILL.md").write_text("x" * 50000)      # over max_bytes (4)
    (adapter / "ADAPTER.md").write_text((root / "ADAPTER.md").read_text() + "\nA learned line.\n")
    result = check_spec(adapter, root)
    checks = {(i.path, i.check) for i in result.issues}
    assert ("collab.py", 1) in checks and ("notes.txt", 2) in checks and ("skills/big/SKILL.md", 4) in checks
    final, repaired = repair(adapter, root, parent)
    assert final.passed, final.issues
    assert {r["path"] for r in repaired} >= {"collab.py", "notes.txt", "skills/big/SKILL.md"}
    assert "A learned line." in (adapter / "ADAPTER.md").read_text()      # the valid change survives
    assert not (adapter / "skills" / "big").exists()


def test_metadata_change_is_restored_from_the_root(tmp_path):
    root = TEMPLATES / "prompt_only"
    adapter = copy(root, tmp_path / "adapter")
    meta = adapter / ".hermit" / "adapter.yaml"
    meta.write_text(meta.read_text().replace("max_bytes: 40000", "max_bytes: 99999"))   # loosen its own limit
    (adapter / ".hermit" / "notes.md").write_text("added\n")
    result = check_spec(adapter, root)
    assert {(i.path, i.check) for i in result.issues} >= {(".hermit/adapter.yaml", 1), (".hermit/notes.md", 1)}
    final, _ = repair(adapter, root, root)
    assert final.passed and meta.read_text() == (root / ".hermit" / "adapter.yaml").read_text()
    assert not (adapter / ".hermit" / "notes.md").exists()


def test_the_card_is_compared_with_the_parent(tmp_path):
    root = TEMPLATES / "prompt_only"
    parent = copy(root, tmp_path / "parent")
    (parent / ".hermit" / "CARD.md").write_text("# Adapter card: earlier run\n")
    adapter = copy(parent, tmp_path / "adapter")
    assert check_spec(adapter, root, parent=parent).passed               # the Trainer's card is not the root's
    (adapter / ".hermit" / "CARD.md").write_text("# Adapter card: rewritten by the learner\n")
    result = check_spec(adapter, root, parent=parent)
    assert not result.passed and (".hermit/CARD.md", 1) in {(i.path, i.check) for i in result.issues}
    final, _ = repair(adapter, root, parent)
    assert final.passed and (adapter / ".hermit" / "CARD.md").read_text() == "# Adapter card: earlier run\n"


def test_exports_are_learnable_and_restored_when_they_do_not_parse(tmp_path):
    from hermit.adapter.loader import load_manifest
    root = Path(__file__).resolve().parents[1] / "docs" / "_examples" / "file_adapter"
    parent = copy(root, tmp_path / "parent")
    exports = parent / "exports.yaml"
    exports.write_text(exports.read_text().replace("Count whitespace-separated words in text.", "Count the words."))
    assert check_spec(parent, root).passed                                 # a tool description is content
    assert load_manifest(parent)["tools"][0]["description"] == "Count the words."
    adapter = copy(parent, tmp_path / "adapter")
    (adapter / "exports.yaml").write_text("tools: [unclosed\n")
    result = check_spec(adapter, root)
    assert not result.passed and ("exports.yaml", 3) in {(i.path, i.check) for i in result.issues}
    final, _ = repair(adapter, root, parent)
    assert final.passed and (adapter / "exports.yaml").read_text() == exports.read_text()


def test_adapter_md_may_be_removed(tmp_path):
    from hermit.adapter.loader import Adapter
    root = TEMPLATES / "prompt_only"
    adapter = copy(root, tmp_path / "adapter")
    (adapter / "ADAPTER.md").unlink()
    assert check_spec(adapter, root).passed, check_spec(adapter, root).issues
    assert Adapter(adapter).context() == ""


def test_no_structure_means_every_file_is_learnable():
    from hermit.adapter.manifest import structure_of
    structure = structure_of({})
    assert structure.learnable_for("a/b/tool.py") and structure.learnable_for("ADAPTER.md")
    assert structure.learnable_for(".hermit/adapter.yaml") is None         # the metadata never is


def test_context_name_is_fixed(tmp_path):
    import pytest
    from hermit.adapter.manifest import read_manifest
    a = tmp_path / "a"; (a / ".hermit").mkdir(parents=True)
    (a / ".hermit" / "adapter.yaml").write_text("template: {id: x, version: '1'}\n")
    (a / "exports.yaml").write_text("context: OTHER.md\n")
    with pytest.raises(ValueError, match="always ADAPTER.md"):
        read_manifest(a)
