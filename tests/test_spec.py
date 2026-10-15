"""check_spec and repair on the two built-in templates."""
from pathlib import Path
import shutil

from harnessadapter.adapter.manifest import matches
from harnessadapter.spec import check_spec, repair

TEMPLATES = Path(__file__).resolve().parents[1] / "harnessadapter" / "algorithms" / "templates"


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


def test_manifest_section_change_is_restored(tmp_path):
    root = TEMPLATES / "prompt_only"
    adapter = copy(root, tmp_path / "adapter")
    text = (adapter / "adapter.yaml").read_text().replace("max_bytes: 40000", "max_bytes: 99999")
    (adapter / "adapter.yaml").write_text(text)
    result = check_spec(adapter, root)
    assert not result.passed and any(i.check == 3 for i in result.issues)
    final, _ = repair(adapter, root, root)
    assert final.passed


def test_adapter_md_is_required(tmp_path):
    root = TEMPLATES / "prompt_only"
    adapter = copy(root, tmp_path / "adapter")
    (adapter / "ADAPTER.md").unlink()
    result = check_spec(adapter, root)
    assert not result.passed and any(i.path == "ADAPTER.md" and i.check == 3 for i in result.issues)
    final, _ = repair(adapter, root, root)
    assert final.passed and (adapter / "ADAPTER.md").exists()


def test_context_name_is_fixed(tmp_path):
    import pytest
    from harnessadapter.adapter.manifest import read_manifest
    a = tmp_path / "a"; a.mkdir()
    (a / "adapter.yaml").write_text("template: {id: x, version: '1'}\nexports: {context: OTHER.md}\nstructure: {}\n")
    with pytest.raises(ValueError, match="always ADAPTER.md"):
        read_manifest(a)
