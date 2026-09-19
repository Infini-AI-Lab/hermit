"""Where the package, its bundled methods, and run-time outputs live."""
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
EVOLVERS = PACKAGE_ROOT / "evolvers"              # bundled Evolver presets
ARCHITECTURES = PACKAGE_ROOT / "architectures"    # bundled starting structures
BUILTIN_ARCHITECTURES = {"prompt-only": ARCHITECTURES / "prompt_only", "mwm": ARCHITECTURES / "mwm"}
BUILTIN_EVOLVERS = {"basic", "mwm"}


def repo_root() -> Path | None:
    """The source checkout this package runs from, if it is one (editable
    install or a plain checkout); None for a site-packages install."""
    parent = PACKAGE_ROOT.parent
    return parent if (parent / "pyproject.toml").is_file() else None


def workspace_root() -> Path:
    """Default parent for generated outputs (.demo/): the checkout when
    running from source, else the current directory."""
    return (repo_root() or Path.cwd()) / ".demo"


def examples_root() -> Path | None:
    root = repo_root()
    return root / "examples" if root else None


def resolve_architecture(reference: str) -> Path:
    """A bundled name (prompt-only, mwm, minimal-shop) or a local path."""
    if reference in BUILTIN_ARCHITECTURES:
        return BUILTIN_ARCHITECTURES[reference]
    if reference == "minimal-shop" and examples_root():
        return examples_root() / "minimal_shop"
    return Path(reference)


def resolve_evolver(reference: str) -> Path:
    """A bundled preset name or a local Evolver definition directory."""
    bundled = EVOLVERS / reference
    return bundled if bundled.is_dir() else Path(reference)
