"""/evidence/README.md — the venue's own description of its evidence system.

Design (world-model-design.md Part 3.3, 2026-09-06): the SHAPE of the
evidence is task-specific and provided with the task; the INTERFACE is
general — the contract says "read /evidence/README.md first", the learner
reads it, then reads the container as it describes. Shared code never
opens the container; only the venue's evidence code and the learner do.

The README text is the venue's former `env_evidence` contract slot, moved
here verbatim: information only (where the records are, what one record
holds, the address form, what a block's panel carries), and — like the
evidence itself — nothing beyond what the host could reach.
"""

from __future__ import annotations

import os

DEPLOYMENTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "deployments")


def _env_evidence(domain: str) -> str:
    """The venue's env_evidence text from the task's deployment directory
    (tasks/CLBench/deployments/<domain>/contract/env_evidence.md), the same
    file the native contract renderer served as a slot."""
    with open(os.path.join(DEPLOYMENTS, domain, "contract", "env_evidence.md")) as f:
        return f.read()


# General to every venue (sub-agent system, 2026-09-07): how a sub-agent's
# work appears in the record. Information only.
SUBAGENT_NOTE = (
    "Sub-agents: a turn a sub-agent took in the host's seat carries "
    "`by: <spec>`; each sub-agent's full trajectory (its calls and "
    "reasoning) is a file under /evidence/delegates/."
)


def text(domain: str) -> str:
    return _env_evidence(domain).rstrip("\n") + "\n\n" + SUBAGENT_NOTE + "\n"


def write(evidence_dir: str, domain: str) -> str:
    """Write README.md into the evidence store (mounted at /evidence).
    Idempotent, atomic; returns the path."""
    os.makedirs(evidence_dir, exist_ok=True)
    p = os.path.join(evidence_dir, "README.md")
    body = text(domain)
    try:
        if open(p).read() == body:
            return p
    except OSError:
        pass
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        f.write(body)
    os.replace(tmp, p)
    return p
