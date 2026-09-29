"""Where the package, its bundled methods, and run-time outputs live, and how
a `module:name` or `file.py:name` reference is loaded."""
import hashlib
import importlib
import importlib.util
from pathlib import Path
import sys

PACKAGE_ROOT = Path(__file__).resolve().parent
EVOLVERS = PACKAGE_ROOT / "evolvers"              # bundled Evolver presets
ARCHITECTURES = PACKAGE_ROOT / "architectures"    # bundled starting structures
BUILTIN_ARCHITECTURES = {"prompt-only": ARCHITECTURES / "prompt_only", "mwm": ARCHITECTURES / "mwm", "animation-workshop": ARCHITECTURES / "animation_workshop", "hier-search": ARCHITECTURES / "hier_search", "lemma-board": ARCHITECTURES / "lemma_board"}
BUILTIN_EVOLVERS = {"basic", "mwm", "animation-coach", "physics-builder", "physics-builder-v2", "animation-workshop", "hier_search", "gepa", "skillopt"}


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


def load_object(spec: str, root: Path | None = None):
    """An attribute named by `module:name` or `path/to/file.py:name`. A file
    path is relative to root (default: the current directory) and, when a
    root is given, must stay inside it. A file is loaded once per process."""
    target, sep, attribute = spec.rpartition(":")
    if not sep or not target or not attribute.isidentifier():
        raise ValueError(f"Expected module:name or file.py:name, got {spec!r}")
    if not target.endswith(".py"):
        return getattr(importlib.import_module(target), attribute)
    base = Path(root).resolve() if root is not None else Path.cwd()
    path = (base / target).resolve()
    if (root is not None and not path.is_relative_to(base)) or not path.is_file():
        raise ValueError(f"No Python file {target} " + (f"inside {base}" if root is not None else ""))
    name = "harnessadapter_loaded_" + hashlib.sha256(str(path).encode()).hexdigest()[:12]
    if name not in sys.modules:
        module_spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(module_spec)
        sys.modules[name] = module
        try:
            module_spec.loader.exec_module(module)
        except BaseException:
            del sys.modules[name]
            raise
    return getattr(sys.modules[name], attribute)
