#!/bin/bash
# The OfficeQA paper's scoring, after the agent: /tests/judge.py scores the final message with the OfficeQA
# repository's reward.py and writes /logs/verifier/reward.txt, reward.json and feedback.txt.
# A judge that fails writes no reward: the run records agent_error (verifier_failed), scored 0 as Harbor grades it.
mkdir -p /logs/verifier
python3 -I -S /tests/judge.py     # isolated: no site, user site or PYTHON* variable the agent may have left runs
