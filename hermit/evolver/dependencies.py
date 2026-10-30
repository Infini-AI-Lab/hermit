"""The pinned upstream packages of the optional Evolvers, and the error an Evolver raises when they are missing.

`pyproject.toml` declares the same requirements as dependency groups, which pip installs only from a checkout
(`python -m pip install -e . --group <name>`, pip 25.1+). The groups are not in the published wheel's metadata, so
a user who installed hermit-harness from PyPI needs the requirements themselves; the error names both ways.
tests/test_optimizer_evolvers.py checks that these requirements equal the dependency groups.
"""
from __future__ import annotations

METHOD_REQUIREMENTS = {
    "gepa": ("gepa @ git+https://github.com/gepa-ai/gepa@d771eb21b5dd3228bc3f567293d2ccfc423fc900",),
    "skillopt": (
        "skillopt @ git+https://github.com/microsoft/SkillOpt@79124b37e9a6371e13b753f8bcd7adb1e493ade1",
        "json_repair>=0.61.0",
    ),
    "harness2": ("harness2 @ git+https://github.com/google-research/harness2@1cfe254e8d25b15f37048c3730134686abe60335",),
    "ace": ("sentence-transformers>=2.2.0",),       # only for use_bulletpoint_analyzer
}


def missing_dependency(evolver: str, needs: str, group: str) -> RuntimeError:
    """The error for an Evolver whose dependency group `group` is not installed; `needs` names what is missing."""
    requirements = " ".join(f"'{r}'" for r in METHOD_REQUIREMENTS[group])
    return RuntimeError(
        f"the {evolver} Evolver needs {needs}. From a Hermit checkout (pip 25.1+): "
        f"python -m pip install -e . --group {group}. Without a checkout: python -m pip install {requirements}")
