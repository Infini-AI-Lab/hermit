"""examples/mwm/visual_showcase/template: the old W0 bytes, its manifest and template check, the wm and context_package
exports, and a Trainer tree made from it. Offline: no model calls.

The tests marked `needs_old` also compare against the old experiment's files (read-only); they run only when
VISUAL_OLD_EXPERIMENT is set to the old experiment's directory (glm-visual-v2-work).
"""
import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil

import pytest
import yaml

from harnessadapter.adapter.loader import Adapter
from harnessadapter.adapter.manifest import files_of, read_manifest, structure_of
from harnessadapter.runtime.python_sandbox import PythonSandbox
from harnessadapter.spec import check_spec, repair
from harnessadapter.tree import ROOT_TAG, Tree, git

TEMPLATE = Path(__file__).resolve().parents[2] / "examples" / "mwm" / "visual_showcase" / "template"
OLD = Path(os.environ.get("VISUAL_OLD_EXPERIMENT") or "")
SESSION = OLD / "evolution" / "sessions" / "evolve-exkatk8q"
needs_old = pytest.mark.skipif(not os.environ.get("VISUAL_OLD_EXPERIMENT") or not SESSION.is_dir(),
                               reason="set VISUAL_OLD_EXPERIMENT to the old experiment's directory")

# W0 as the old experiment froze it (evolution/source-before.json, bytecode caches left out). adapter.json is the one
# W0 file the template replaces (by adapter.yaml); export.py and check.py are new.
W0_SHA256 = {
    "ADAPTER.md": "1d56b976584d3fcbaa0d4cebd2f9d0e3b18ff5dff7d16397fb246b783daeca01",
    "EVOLVER.md": "ca8bf9464a32ba0cc87e296b90e16af56587b6de2494188d945fe364b32b4fa5",
    "README.md": "6a3d9a26ec4c4c799abf0db9905b12e462094b8c8ee3acee66da50a2060ec6d8",
    "agents/README.md": "ea27217018a2429358eeb51c8a815a2a1089feec7fa4b1bde405839ba8b3f468",
    "agents/_template/SPEC.md": "cd1b320c2e493a75a7b13e841ed0642bd85c29e0e7ffe0731cdb69fb64c009b6",
    "consumption/brief.md": "b65e61aaecac5bfa88ed6b3174afe6206a592c71889909a377e154e49ace3d65",
    "examples/README.md": "7913cb401f6a8e7db42029f1898d042716a0773eee813c49fb1f5d3413b6d72f",
    "examples/_template.md": "72a0bfedb301f8df236553236eb73b9f73420715545e2469f00b504d36e9f10a",
    "interface.py": "6c8f9be9906c94819a89210f6618fb744b46838607b35e10d36a0d9ae7cb2985",
    "memory/README.md": "739ccfe27805320436772a0e00d6e2c1a738d954dc197a0bf48a1fabefe7ff5a",
    "memory/_template.md": "812b202f4772b8b0fce5effbd09141e609a345556b2dc0c2094f61a0282c7de1",
    "meta/cases/README.md": "881e9b3ee530935498c5b89ff2c32d7d60645229335d6b972b0f3c6c80f9525a",
    "meta/journal.md": "300a8c28eb0cc33c616cbe53aa06f1894185cfb19c91638e6c74e46fce27070e",
    "mwm_core/__init__.py": "403a058f532fbc613fa8325a2f869f9f436cc4028709759fbd65dcd7007de668",
    "mwm_core/brief.py": "0f0e3985f16e8e4e156b374d0c238ed274e13cbb5fbffe7575bf9345ef3cb0d5",
    "mwm_core/capabilities.py": "ddcd451df990b0fdb1ef04cf84a9f43c1306e45387fbeb579904ce46d8b60ee6",
    "mwm_core/clbench_recall.py": "a4a6c6624bbc16b6fef031c772794622b44bb4ff6dd6dab4caf24706e21a60f2",
    "mwm_core/delegate.py": "df131440deb2ada5f77fef6a88c7a953cdd36e0d95288486959754d58ebfb6b9",
    "mwm_core/delegation.py": "26ebe8c91c66310a0b394cefbad1ba9af8133db2efa8e78701f1823de8eaee75",
    "mwm_core/host_framing.py": "128482090b0c564ec960bbcf704d261ba7590adedbe0b22c705254da85ceef4c",
    "mwm_core/sandbox.py": "d7c2251c41acf2ee4d706f9e05407a0e3a3e3026781b768aeb660805a222d059",
    "mwm_core/staleness.py": "cfa74536fa067eb637e4dbd0c581d4284370fc45c353aa6e378ea719f5379cbd",
    "mwm_core/trace.py": "e2818164d590c914f3f00e6c9a473250807eb4d48447344362cb5fb17112b4c4",
    "mwm_core/wm_iface.py": "d3e96ede920b69a10b016f1c52720e3ddbbbef7897208213297114aadfb64151",
    "mwm_core/wm_tools.py": "1c09cf68a8378785c7dfbf30a84adc389737d872517b8f855cc8798edd9d47eb",
    "tools/README.md": "8219aada69cfe75956b5464a1d307788ad327369971ff7ef3a38b90a7821a88b",
    "tools/_template/TOOL.md": "3a080020a9f8ff3e011876ca377422734456f3d99752f7b966c5feef5dfe1d11",
    "tools/_template/run.py": "e900db6fca9783508e36468089b67abf37577e89dae929ad6fe1d97af56e1af8",
    "tools/_template/test.py": "950ec92aea1aefdc051b3ca86503624397ece17621718f89762b95930addb1e6",
}
NEW_FILES = {"adapter.yaml", "export.py", "check.py"}
OLD_ADAPTER_JSON_SHA256 = "bd6fba5776991bdfafec30cf58bf741cc307c1dca701844d8888f6bfb968bd9a"
# sha256 of json.dumps(old adapter.json tools[0], sort_keys=True): the wm declaration old harness.py offered.
WM_DECLARATION_SHA256 = "8480d94f1235116af37713e522d1799522860bfa73ed71fb50127700155f3477"
# adapter-context.md: the package old arm C appended to its system prompt (deployment.json context_sha256).
OLD_CONTEXT_SHA256 = "0222a9beb40870c20d5a7687e7d54698eed01381bfc9dc220a0b547772948ade"
KNOWLEDGE = {"consumption/brief.md", "examples/README.md", "examples/_template.md", "memory/README.md",
             "memory/_template.md", "meta/cases/README.md", "meta/journal.md"}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def copy_template(dst: Path) -> Path:
    # Load copies only: importing mwm_core in place could leave bytecode caches in the template.
    return shutil.copytree(TEMPLATE, dst, ignore=shutil.ignore_patterns("__pycache__"))


def call(adapter: Adapter, tool: str, arguments: dict):
    return asyncio.run(adapter.call(tool, arguments))


def wm(adapter: Adapter, name: str, args: dict | None = None) -> str:
    result = call(adapter, "wm", {"name": name, "args": args or {}})
    assert set(result) == {"text"} and isinstance(result["text"], str)
    return result["text"]


def sandbox_available() -> bool:
    return PythonSandbox(TEMPLATE).status()["available"]


# ---------- files and manifest ----------

def test_w0_files_are_byte_identical():
    have = set(files_of(TEMPLATE))
    assert have == set(W0_SHA256) | NEW_FILES
    changed = [rel for rel, digest in W0_SHA256.items() if sha256(TEMPLATE / rel) != digest]
    assert not changed


@needs_old
def test_w0_files_match_the_old_history():
    history = SESSION / "history"
    old = set(files_of(history)) - {"adapter.json"}
    assert old == set(W0_SHA256)
    assert all((TEMPLATE / rel).read_bytes() == (history / rel).read_bytes() for rel in old)
    assert sha256(history / "adapter.json") == OLD_ADAPTER_JSON_SHA256


def test_manifest_sections_exports_and_no_origin():
    data = yaml.safe_load((TEMPLATE / "adapter.yaml").read_text())
    assert set(data) == {"template", "exports", "structure", "check"}     # no origin: the pipeline's arm-B marker
    manifest = read_manifest(TEMPLATE)
    assert manifest["template"]["id"] == "visual-mwm" and manifest["check"] == "check.py:check"
    tools = {t["name"]: t for t in manifest["exports"]["tools"]}
    assert list(tools) == ["wm", "context_package"]
    wm_declaration = json.dumps(tools["wm"], sort_keys=True).encode()
    assert hashlib.sha256(wm_declaration).hexdigest() == WM_DECLARATION_SHA256
    assert tools["context_package"]["entrypoint"] == "export.py:context_package"
    assert tools["context_package"]["input_schema"]["properties"] == {}


def test_manifest_text_is_what_the_trainer_writes():
    """The learner can read adapter.yaml: no commentary in it, and in yaml.safe_dump form, so the Trainer's first
    commit (which rewrites it to add `origin`) only appends the origin lines."""
    text = (TEMPLATE / "adapter.yaml").read_text()
    data = yaml.safe_load(text)
    assert "#" not in text and yaml.safe_dump(data, sort_keys=False, allow_unicode=True) == text
    data["origin"] = {"template": data["template"]["id"], "version": data["template"]["version"]}
    assert yaml.safe_dump(data, sort_keys=False, allow_unicode=True) == \
        text + "origin:\n  template: visual-mwm\n  version: '1.0'\n"


@needs_old
def test_wm_declaration_is_the_old_one():
    old = json.loads((SESSION / "history" / "adapter.json").read_text())["tools"][0]
    assert read_manifest(TEMPLATE)["exports"]["tools"][0] == old
    deployed = json.loads((OLD / "deployment.json").read_text())["wm_declaration"]
    assert deployed == old


def test_structure_learnable_and_fixed():
    structure = structure_of(read_manifest(TEMPLATE))
    learnable = {rel for rel in files_of(TEMPLATE) if structure.learnable_for(rel)}
    assert learnable == KNOWLEDGE
    assert all(structure.is_fixed(rel) for rel in set(files_of(TEMPLATE)) - KNOWLEDGE)
    assert structure.learnable_for("consumption/brief.md").format == {"text": True, "max_bytes": 6000}
    for rel in ("memory/ocean.md", "examples/waves.md", "examples/a/b.md", "meta/journal-archive.md",
                "meta/cases/01-x.md", "meta/cases/a/b.md"):
        assert structure.learnable_for(rel) is not None, rel
    for rel in ("memory/x.py", "meta/notes.md", "consumption/other.md", "tools/x/run.py", "agents/x/SPEC.md",
                "export.py", "check.py", "interface.py", "mwm_core/x.py", "ADAPTER.md", "notes.md"):
        assert structure.learnable_for(rel) is None, rel


# ---------- check_spec and the template check ----------

def test_check_spec_passes_on_the_root():
    result = check_spec(TEMPLATE, TEMPLATE)
    assert result.passed, result.issues


def test_template_check_finds_and_repair_reverts(tmp_path):
    adapter = copy_template(tmp_path / "adapter")
    (adapter / "memory" / "page.md").write_text("brief: a copy\n\n<!DOCTYPE html>\n<html lang='en'></html>\n")
    (adapter / "examples" / "waves.md").write_text("# waves\n\nA <html> tag mentioned in prose.\n")
    (adapter / "memory" / ".hidden.md").write_text("brief: hidden\n")
    (adapter / "consumption" / "brief.md").write_text("x" * 6001)
    (adapter / "meta" / "journal.md").write_text((TEMPLATE / "meta" / "journal.md").read_text() + "\nA learned line.\n")
    result = check_spec(adapter, TEMPLATE)
    found = {(i.path, i.check) for i in result.issues}
    assert found == {("memory/page.md", 6), ("examples/waves.md", 6), ("memory/.hidden.md", 6),
                     ("consumption/brief.md", 4), ("consumption/brief.md", 6)}
    final, repaired = repair(adapter, TEMPLATE, TEMPLATE)
    assert final.passed, final.issues
    assert {r["path"] for r in repaired} == {"memory/page.md", "examples/waves.md", "memory/.hidden.md",
                                             "consumption/brief.md"}
    assert not (adapter / "memory" / "page.md").exists()
    assert sha256(adapter / "consumption" / "brief.md") == W0_SHA256["consumption/brief.md"]
    assert "A learned line." in (adapter / "meta" / "journal.md").read_text()       # the valid change survives


def test_brief_cap_counts_characters(tmp_path):
    adapter = copy_template(tmp_path / "adapter")
    spec = importlib.util.spec_from_file_location("visual_mwm_check", adapter / "check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    brief = adapter / "consumption" / "brief.md"
    brief.write_text("é" * 6000)                    # 6000 characters, 12000 bytes: the old character cap passes
    assert module.check(str(adapter)) == []
    brief.write_text("é" * 6001)
    assert module.check(str(adapter)) == [{"path": "consumption/brief.md",
                                           "problem": "brief has 6001 characters; maximum is 6000"}]


def test_fixed_files_cannot_change(tmp_path):
    adapter = copy_template(tmp_path / "adapter")
    (adapter / "mwm_core" / "wm_tools.py").write_text((adapter / "mwm_core" / "wm_tools.py").read_text() + "\n# x\n")
    (adapter / "tools" / "mine").mkdir()
    (adapter / "tools" / "mine" / "run.py").write_text("def run():\n    return 1\n")
    found = {(i.path, i.check) for i in check_spec(adapter, TEMPLATE).issues}
    assert ("mwm_core/wm_tools.py", 1) in found and ("tools/mine/run.py", 2) in found


# ---------- the exports on W0 ----------

def test_wm_and_context_package_on_w0(tmp_path):
    adapter = Adapter(copy_template(tmp_path / "w0"))
    assert set(adapter.tools) == {"wm", "context_package"}
    assert wm(adapter, "list").startswith("The world model is a repository maintained between episodes")
    assert "Layout of /world-model (now):" in wm(adapter, "list")
    assert wm(adapter, "recall", {"address": "waves"}) == "no case matches"
    assert "/world-model/examples/README.md:10: " in wm(adapter, "search", {"query": "_template.md"})
    assert wm(adapter, "read", {"path": "/world-model/memory/"}) == "README.md\n_template.md"
    assert wm(adapter, "read", {"path": "/world-model/consumption/brief.md"}).startswith("# Environment briefing")
    assert wm(adapter, "memory", {"id": "x"}) == "no memory entry named 'x'"
    assert wm(adapter, "trace", {"address": "waves"}) == "trace: no evidence resource is bound for this deployment"
    text = call(adapter, "context_package", {})["text"]
    assert text.startswith("You are working in an environment you will encounter repeatedly.")
    for section in ("=== Environment briefing ===\n# Environment briefing", "=== Open questions ===\n(none)",
                    "=== World-model memory (catalog) ===\n(empty)", "=== Reaching the world model ==="):
        assert section in text
    if not sandbox_available():       # macOS, as in the old run: the package says so, like old adapter-context.md
        assert text.endswith("wm run is unavailable.")


# ---------- the old learned adapter (W_glm) under the new template ----------

def build_w_glm(dst: Path) -> Path:
    """The template with the old learner's knowledge files, copied with their modification times."""
    adapter = copy_template(dst)
    structure = structure_of(read_manifest(TEMPLATE))
    learned = SESSION / "adapter"
    for rel in files_of(learned):
        if rel == "adapter.json":
            continue
        if structure.learnable_for(rel):
            (adapter / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(learned / rel, adapter / rel)
        else:
            assert (learned / rel).read_bytes() == (TEMPLATE / rel).read_bytes(), rel
    return adapter


@needs_old
def test_old_learned_adapter_conforms_and_renders_the_old_package(tmp_path):
    adapter = build_w_glm(tmp_path / "w_glm")
    result = check_spec(adapter, TEMPLATE)
    assert result.passed, result.issues
    old = (OLD / "adapter-context.md").read_text()
    assert hashlib.sha256(old.encode()).hexdigest() == OLD_CONTEXT_SHA256
    loaded = Adapter(adapter)
    text = call(loaded, "context_package", {})["text"]
    if not sandbox_available():
        # With the old files' modification times the catalog order is the old one: byte-identical.
        assert text == old and len(text.encode()) == 12428
    assert wm(loaded, "recall", {"address": "waves"}).startswith('input: "Ocean waves" showcase brief')
    assert wm(loaded, "memory", {"id": "ocean-waves-visuals"}).startswith(
        (adapter / "memory" / "ocean-waves-visuals.md").read_text()[:200])


@needs_old
def test_catalog_order_nit_with_equal_modification_times(tmp_path):
    """git archive gives every file of an exported adapter the commit's time. The catalog and the open questions
    are ordered newest edit first (git log, else modification time), so with equal times only their order can
    differ from the old package: same bytes, same lines."""
    adapter = build_w_glm(tmp_path / "w_glm")
    for rel in files_of(adapter):
        os.utime(adapter / rel, ns=(1_700_000_000_000_000_000,) * 2)
    text = call(Adapter(adapter), "context_package", {})["text"]
    old = (OLD / "adapter-context.md").read_text()
    if not sandbox_available():
        assert len(text.encode()) == len(old.encode()) == 12428
        assert sorted(text.splitlines()) == sorted(old.splitlines())
        # Framing and briefing before the open questions, and the reach section, are unchanged.
        assert text.split("=== Open questions ===")[0] == old.split("=== Open questions ===")[0]
        assert text.split("=== Reaching the world model ===")[1] == old.split("=== Reaching the world model ===")[1]


# ---------- a Trainer tree ----------

def test_trainer_tree_from_the_template(tmp_path):
    from harnessadapter.trainer.trainer import _set_origin
    tree = Tree.open_or_create(tmp_path / "tree", TEMPLATE)
    assert "origin" not in tree.root_manifest()
    committed = git(tree.repo, "ls-tree", "-r", "--name-only", ROOT_TAG).splitlines()
    assert sorted(committed) == sorted(files_of(TEMPLATE))
    root = tree.export(ROOT_TAG, tmp_path / "root")
    assert check_spec(root, TEMPLATE).passed
    assert Tree.open_or_create(tmp_path / "tree", TEMPLATE).repo == tree.repo        # reopens: same template
    adapter = tree.export(ROOT_TAG, tmp_path / "adapter")
    assert "=== World-model memory (catalog) ===" in call(Adapter(adapter), "context_package", {})["text"]
    # The Trainer names the origin when it commits a learned change; the adapter still conforms to the root.
    (adapter / "memory" / "lesson.md").write_text("brief: a learned lesson\n\nText.\n")
    _set_origin(adapter, read_manifest(root)["template"])
    assert read_manifest(adapter)["origin"] == {"template": "visual-mwm", "version": "1.0"}
    assert check_spec(adapter, root).passed
