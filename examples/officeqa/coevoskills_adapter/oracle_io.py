"""OfficeQA's hand-over from the acting agent to the hidden test of prepare.py's task packages: the agent's answer
(`output["answer"]`, ../utils/task.py:answer) in <answer> tags at /root/answer.txt, which tests/test_outputs.py reads.

    evolver:
      params:
        deliver: oracle_io.py:deliver
"""

from pathlib import Path

from hermit.agent.evolver.methods.coevoskills_adapter.integrity import record_deployment
from hermit.task import load_callable

officeqa_collect = load_callable("../utils/task.py:collect", Path(__file__).parent)


def collect(task: dict, workspace: Path, log: Path) -> dict:
    """Keep the OfficeQA task log and check deployed skills in the acting workspace before it is released."""
    facts = officeqa_collect(task, workspace, log)
    record_deployment(workspace, log)
    return facts


def deliver(output) -> dict:
    answer = str((output or {}).get("answer") or "") if isinstance(output, dict) else str(output or "")
    return {"/root/answer.txt": f"<answer>{answer}</answer>"}
