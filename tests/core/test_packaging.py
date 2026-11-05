"""Bundled templates and methods include their resources, including files skipped by ordinary globs."""
from glob import glob
from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "hermit"


def _packaged() -> set[str]:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]["package-data"]["hermit"]
    return {Path(p).relative_to(PACKAGE).as_posix()
            for pattern in config for p in glob(str(PACKAGE / pattern), recursive=True) if Path(p).is_file()}


def test_every_template_ships_its_metadata():
    """Every template's adapter.yaml is packaged: the file-coverage test below checks only the files there are."""
    packaged = _packaged()
    for template in (PACKAGE / "adapter" / "templates").iterdir():
        if template.is_dir() and (template / ".hermit").is_dir():
            assert f"adapter/templates/{template.name}/.hermit/adapter.yaml" in packaged, template.name


def test_package_data_covers_every_template_file():
    templates = PACKAGE / "adapter" / "templates"
    files = {p.relative_to(PACKAGE).as_posix() for p in templates.rglob("*")
             if p.is_file() and "__pycache__" not in p.parts and p.suffix not in (".pyc", ".pyo")}
    missing = sorted(files - _packaged())
    assert not missing, missing


def test_package_data_covers_every_method_file():
    methods = PACKAGE / "agent" / "evolver" / "methods"
    files = {p.relative_to(PACKAGE).as_posix() for p in methods.rglob("*")
             if p.is_file() and "__pycache__" not in p.parts and p.suffix not in (".pyc", ".pyo")}
    assert not (missing := sorted(files - _packaged())), missing
