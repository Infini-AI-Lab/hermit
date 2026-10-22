"""/evidence/README.md: the venue's own description of its evidence system.

The SHAPE of the evidence is task-specific and comes with the task (its deployment texts: the contract's
env_evidence slot); the INTERFACE is general: the contract says "read /evidence/README.md first", the learner reads
it, then reads the store as it describes. The acting side writes it before the first episode (the launchers call
this module), and again whenever it writes a record.

    python -m hermit.algorithms.evolvers.mwm.evidence.readme --evidence DIR --from deployment/contract/env_evidence.md
"""
from __future__ import annotations

import argparse
from pathlib import Path

# General to every venue (sub-agent system, 2026-09-07): how a sub-agent's work appears in the record. Information only.
SUBAGENT_NOTE = ("Sub-agents: a turn a sub-agent took in the host's seat carries `by: <spec>`; each sub-agent's "
                 "full trajectory (its calls and reasoning) is a file under /evidence/delegates/.")


def text(env_evidence_text: str) -> str:
    return env_evidence_text.rstrip("\n") + "\n\n" + SUBAGENT_NOTE + "\n"


def write(evidence_dir: str | Path, env_evidence_text: str) -> Path:
    """Write README.md into the evidence store (mounted at /evidence). Idempotent, atomic; returns the path."""
    root = Path(evidence_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "README.md"
    body = text(env_evidence_text)
    if not path.exists() or path.read_text() != body:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(body)
        tmp.replace(path)
    return path


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True, help="the evidence store")
    parser.add_argument("--from", dest="source", type=Path, required=True, help="the venue's env_evidence.md")
    args = parser.parse_args(argv)
    print(write(args.evidence, args.source.read_text()))


if __name__ == "__main__":
    main()
