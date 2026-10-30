#!/usr/bin/env python3
"""The framework's distribution for the offline image, read from the checkout's pyproject.toml (the Dockerfile runs it):

    hermit_dist.py deps PYPROJECT [NAME ...]     print the dependencies, one per line: the core ones and those of
                                                 each named extra or dependency group
    hermit_dist.py register PYPROJECT            write the hermit-harness distribution's metadata, and no files, into
                                                 this interpreter's site-packages

The checkout is not installed in the image: offline.sh mounts it and PYTHONPATH makes the mounted `hermit` import.
hermit/sandbox.py still looks the installed distribution up (`package_closure(["hermit-harness"])`) to learn which
packages a sandboxed process needs, so `register` writes the metadata pip would have written, with the same
Requires-Dist lines, and nothing else.
"""
from __future__ import annotations

from pathlib import Path
import sys
import sysconfig
import tomllib


def pyproject(path: str) -> dict:
    return tomllib.loads(Path(path).read_text())


def project(path: str) -> dict:
    return pyproject(path)["project"]


def group(groups: dict, name: str) -> list[str]:
    """A dependency group's requirements (PEP 735), its `include-group` entries expanded."""
    out = []
    for entry in groups[name]:
        out += group(groups, entry["include-group"]) if isinstance(entry, dict) else [entry]
    return out


def deps(data: dict, names: list[str]) -> list[str]:
    p, groups = data["project"], data.get("dependency-groups", {})
    optional = p.get("optional-dependencies", {})
    out = list(p.get("dependencies", []))
    for name in names:
        if name in optional:
            out += optional[name]
        elif name in groups:
            out += group(groups, name)
        else:
            sys.exit(f"pyproject.toml has no extra or dependency group {name!r} "
                     f"(extras {sorted(optional)}, groups {sorted(groups)})")
    return out


def register(p: dict) -> Path:
    name, version = p["name"], p["version"]
    info = Path(sysconfig.get_paths()["purelib"]) / f"{name.replace('-', '_')}-{version}.dist-info"
    info.mkdir(parents=True, exist_ok=True)
    lines = ["Metadata-Version: 2.1", f"Name: {name}", f"Version: {version}"]
    if p.get("requires-python"):
        lines.append(f"Requires-Python: {p['requires-python']}")
    lines += [f"Requires-Dist: {r}" for r in p.get("dependencies", [])]
    for extra, reqs in p.get("optional-dependencies", {}).items():
        lines.append(f"Provides-Extra: {extra}")
        lines += [f'Requires-Dist: {r} ; extra == "{extra}"' for r in reqs]
    (info / "METADATA").write_text("\n".join(lines) + "\n")
    (info / "INSTALLER").write_text("docker/hermit_dist.py\n")
    (info / "RECORD").write_text("".join(f"{info.name}/{f},,\n" for f in ("METADATA", "INSTALLER", "RECORD")))
    return info


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "deps":
        print("\n".join(deps(pyproject(argv[1]), argv[2:])))
    elif len(argv) == 2 and argv[0] == "register":
        print(f"registered {register(project(argv[1]))}")
    else:
        sys.exit(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
