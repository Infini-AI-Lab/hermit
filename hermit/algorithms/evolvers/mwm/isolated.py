"""The mwm maintenance that executes the adapter's own modules, as entry points a worker runs in the learner's
sandbox (#11): the adapter is the learner's work, so its code runs with the learner's view, never the Trainer's.

Each function takes paths as strings and returns what the Evolver needs as JSON."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent


def _prompt():
    spec = importlib.util.spec_from_file_location("_mwm_isolated_prompt", HERE / "prompt.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def prepare(adapter: str, session: str, block: int, tick: int, evidence: str, goal: str, notes: str) -> dict:
    """Maintenance before a session, then its prompt."""
    prompt = _prompt()
    state = prompt.prepare_session(Path(adapter), Path(session), block=block, tick=tick)
    text = prompt.build_prompt(Path(adapter), Path(evidence), goal, notes=notes, session=Path(session))
    return {"advice": (state or {}).get("advice"), "prompt": text}


def admit(adapter: str, session: str) -> dict:
    """Admission after a session: the learned tools' own tests and the gates."""
    return _prompt().finalize(Path(adapter), Path(session))


def render(adapter: str, deployment: str) -> None:
    """ADAPTER.md: the host package rendered with this deployment."""
    from hermit.adapter.loader import Adapter
    from hermit.algorithms.evolvers.mwm import deployment as _dep
    loaded = Adapter(Path(adapter), {"deployment": _dep.host_settings(Path(deployment))})
    text = loaded._module("interface.py").context(_resources=loaded.resources)
    (Path(adapter) / "ADAPTER.md").write_text(text.rstrip("\n") + "\n")
