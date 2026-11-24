#!/bin/bash
# The user's score, which the acting agent left in its logs (user-reward.txt); the run's judge reads the verdict the
# agent kept on the host instead (../../../../utils/judge.py: user), so this reward is informational.
mkdir -p /logs/verifier
if [ -s /logs/agent/user-reward.txt ]; then
  cp /logs/agent/user-reward.txt /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
