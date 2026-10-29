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
    root = TEMPLATES.parents[2] / "docs" / "_examples" / "file_adapter"     # a template without a check of its own
    adapter = copy(root, tmp_path / "adapter")
    (adapter / "ADAPTER.md").unlink()
    assert check_spec(adapter, root).passed, check_spec(adapter, root).issues
    assert Adapter(adapter).context() == ""


def test_prompt_only_keeps_adapter_md(tmp_path):
    root = TEMPLATES / "prompt_only"
    parent = copy(root, tmp_path / "parent")
    adapter = copy(root, tmp_path / "adapter")
    (adapter / "ADAPTER.md").unlink()
    assert ("ADAPTER.md", 6) in {(i.path, i.check) for i in check_spec(adapter, root).issues}
    final, _ = repair(adapter, root, parent)
    assert final.passed and (adapter / "ADAPTER.md").read_text() == (root / "ADAPTER.md").read_text()


def test_prompt_only_learns_its_prompt_only(tmp_path):
    root = TEMPLATES / "prompt_only"
    adapter = copy(root, tmp_path / "adapter")
    (adapter / "exports.yaml").write_text("context: ADAPTER.md\nskills: skills\n")
    (adapter / "notes.md").write_text("a stray file\n")
    checks = {(i.path, i.check) for i in check_spec(adapter, root).issues}
    assert ("exports.yaml", 1) in checks and ("notes.md", 2) in checks


def test_exports_are_learnable_without_a_learnable_entry(tmp_path):
    import yaml
    from hermit.adapter.manifest import MANIFEST
    root = copy(TEMPLATES / "prompt_only", tmp_path / "root")
    meta = yaml.safe_load((root / MANIFEST).read_text())
    meta["structure"]["fixed"].remove("exports.yaml")                     # in neither list
    (root / MANIFEST).write_text(yaml.safe_dump(meta, sort_keys=False))
    adapter = copy(root, tmp_path / "adapter")
    (adapter / "exports.yaml").write_text("context: ADAPTER.md\nskills: skills\n")
    assert check_spec(root, root).passed and check_spec(adapter, root).passed
    meta["structure"]["fixed"].append("exports.yaml")                     # listed under fixed: no longer learnable
    (root / MANIFEST).write_text(yaml.safe_dump(meta, sort_keys=False))
    shutil.copy(root / MANIFEST, adapter / MANIFEST)
    assert ("exports.yaml", 1) in {(i.path, i.check) for i in check_spec(adapter, root).issues}


def _edit_exports(adapter, edit):
    import yaml
    exports = yaml.safe_load((adapter / "exports.yaml").read_text())
    edit(exports)
    (adapter / "exports.yaml").write_text(yaml.safe_dump(exports, sort_keys=False))


def test_template_checks_keep_the_exported_wiring(tmp_path):
    def describe(e): e["tools"][0]["description"] = "A learned description."
    def drop_tools(e): e["tools"] = []
    def drop_reactions(e): e.pop("reactions")
    def rename_wm(e): e["tools"][0]["name"] = "world"
    for name, edit, kept in (("lean_collab", describe, True), ("lean_collab", drop_tools, False),
                             ("lean_collab", drop_reactions, False), ("mwm", describe, True),
                             ("mwm", rename_wm, False)):
        case = tmp_path / f"{name}-{edit.__name__}"
        root = TEMPLATES / name
        parent = copy(root, case / "parent")
        adapter = copy(root, case / "adapter")
        _edit_exports(adapter, edit)
        assert check_spec(adapter, root, deploy=False).passed is kept, (name, edit.__name__)
        final, _ = repair(adapter, root, parent)
        changed = (adapter / "exports.yaml").read_text() != (root / "exports.yaml").read_text()
        assert final.passed and changed is kept, (name, edit.__name__)


def test_template_checks_restore_a_deleted_exports_file(tmp_path):
    for name in ("lean_collab", "mwm"):
        root = TEMPLATES / name
        parent = copy(root, tmp_path / name / "parent")
        adapter = copy(root, tmp_path / name / "adapter")
        (adapter / "exports.yaml").unlink()                                  # no exports: no tools, no reactions
        assert ("exports.yaml", 6) in {(i.path, i.check) for i in check_spec(adapter, root, deploy=False).issues}
        final, _ = repair(adapter, root, parent)
        assert final.passed and (adapter / "exports.yaml").read_text() == (root / "exports.yaml").read_text()


def test_deploy_and_serve_name_the_0_1_layout_in_one_line(tmp_path, capsys):
    from hermit.cli import main
    adapter = copy(TEMPLATES / "prompt_only", tmp_path / "adapter")
    shutil.move(adapter / ".hermit" / "adapter.yaml", adapter / "adapter.yaml")
    for argv in (["deploy", "--adapter", str(adapter), "--workspace", str(tmp_path / "ws")], ["serve", str(adapter)]):
        assert main(argv) == 2
        err = capsys.readouterr().err
        assert "Hermit 0.1 layout" in err and "Traceback" not in err


def test_the_0_1_layout_is_named(tmp_path):
    import pytest
    from hermit.adapter.manifest import read_manifest
    adapter = copy(TEMPLATES / "prompt_only", tmp_path / "adapter")
    shutil.move(adapter / ".hermit" / "adapter.yaml", adapter / "adapter.yaml")
    with pytest.raises(ValueError, match="Hermit 0.1 layout"):
        read_manifest(adapter)


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
