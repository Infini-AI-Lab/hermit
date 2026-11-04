"""OfficeQA's hand-over from the acting agent to the hidden test of prepare.py's task packages: the agent's answer
(`output["answer"]`, ../utils/task.py:answer) in <answer> tags at /root/answer.txt, which tests/test_outputs.py reads.

    evolver:
      params:
        deliver: oracle_io.py:deliver
"""


def deliver(output) -> dict:
    answer = str((output or {}).get("answer") or "") if isinstance(output, dict) else str(output or "")
    return {"/root/answer.txt": f"<answer>{answer}</answer>"}
