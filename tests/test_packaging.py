"""The wheel carries every template file: setuptools expands package-data with glob, which skips dot-directories."""
from glob import glob
from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "hermit"


def _packaged() -> set[str]:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]["package-data"]["hermit"]
    return {Path(p).relative_to(PACKAGE).as_posix()
            for pattern in config for p in glob(str(PACKAGE / pattern), recursive=True) if Path(p).is_file()}


def test_package_data_covers_every_template_file():
    templates = PACKAGE / "algorithms" / "templates"
    files = {p.relative_to(PACKAGE).as_posix() for p in templates.rglob("*")
             if p.is_file() and "__pycache__" not in p.parts and p.suffix not in (".pyc", ".pyo")}
    missing = sorted(files - _packaged())
    assert not missing, missing


def test_every_template_ships_its_metadata():
    packaged = _packaged()
    for template in (PACKAGE / "algorithms" / "templates").iterdir():
        if template.is_dir() and (template / ".hermit").is_dir():
            assert f"algorithms/templates/{template.name}/.hermit/adapter.yaml" in packaged, template.name
