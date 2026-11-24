"""The judges of a session with a user (user.py): the user's verdict as the score, or the task's verifier as the score
with the user's verdict beside it.

    judge: {path: ../utils/judge.py, name: user}       # AHE-Beta's evolution: the user's rating, never a verifier's
    judge: {path: ../utils/judge.py, name: verified}   # a held-out evaluation: pass@1 from the task's own verifier

`user` scores the verdict the acting agent kept on this machine (`RunOutput.extra["user"]`), not a file in the task's
environment, which the agent's commands could have written. Its feedback is what the user said of the session (the
rating, the outcome, the reason, the issues): what AHE-Beta's requirement roles start from. The verifier's verdict,
which the run takes anyway, is neither score nor feedback, so a learner under AHE-Beta's protocol never reads a
benchmark's tests through it (an AHE-Beta exported task's verifier only copies the rating).

`verified` is hermit.task.verify's environment judge (the task's tests decide the score: pass@1), with the user's
rating and outcome added to its record when the session had a user.

Both refuse a rollout that is not the agent's result (its environment, its session or its user failed: `unscored`):
the run records it as a failed run, never as the agent's 0. A session the adapter failed before Codex started is the
agent's result, scored 0 with its message.
"""
from __future__ import annotations

import json

from hermit.common import RunOutput
from hermit.task.data import JudgeResult
from hermit.task.verify import Unscored, environment

ISSUES = 20                      # issues quoted in the feedback, at most
TEXT = 2000                      # characters of one field quoted in the feedback, at most


def _cut(value) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= TEXT else text[:TEXT] + f" ... [{len(text) - TEXT} more characters]"


def _issues(verdict: dict) -> list[str]:
    raw = verdict.get("issues")
    if raw is None and isinstance(verdict.get("feedback"), dict):
        raw = verdict["feedback"].get("issues")
    out = []
    for issue in (raw or [])[:ISSUES]:
        if isinstance(issue, dict):
            title = issue.get("title") or issue.get("summary") or ""
            body = issue.get("body") or issue.get("description") or ""
            out.append(f"- {_cut(title)}" + (f": {_cut(body)}" if body else ""))
        else:
            out.append(f"- {_cut(issue)}")
    return out


def _said(verdict: dict) -> str:
    """What the user said of the session, as a learner reads it."""
    lines = []
    if verdict.get("rating") is not None:
        lines.append(f"user rating: {verdict['rating']} (score {float(verdict['score']):g})")
    for key in ("outcome", "reason"):
        if verdict.get(key):
            lines.append(f"{key}: {_cut(verdict[key])}")
    if isinstance(verdict.get("feedback"), (str, dict)) and verdict.get("feedback"):
        feedback = verdict["feedback"]
        if isinstance(feedback, dict):
            feedback = {k: v for k, v in feedback.items() if k != "issues"}
        if feedback:
            lines.append(f"feedback: {_cut(feedback)}")
    issues = _issues(verdict)
    if issues:
        lines += ["issues the user raised:", *issues]
    return "\n".join(lines)


def _refused(output: RunOutput) -> None:
    extra = output.extra or {}
    why = extra.get("unscored") or ((extra.get("verified") or {}).get("status") == "infra" and "infra")
    if why:
        message = str(extra.get("exception_message") or (extra.get("verified") or {}).get("exception_message") or "")
        raise Unscored(f"the rollout is not the agent's result ({why}{': ' + message[:600] if message else ''}): it "
                       "has no score")


def user(task: dict, output: RunOutput) -> JudgeResult:
    """The user's verdict: its score (in [0, 1]: AHE-Beta's (rating - 1) / 3), and what the user said as feedback."""
    _refused(output)
    extra = output.extra or {}
    if extra.get("adapter_failed"):
        return JudgeResult(0.0, f"the adapter failed before the session: {extra.get('exception_message') or ''}",
                           {"status": "agent_error", "failure_cause": extra["adapter_failed"]})
    verdict = extra.get("user")
    if not isinstance(verdict, dict) or verdict.get("error") or verdict.get("score") is None:
        raise Unscored("the session has no verdict of its user: it has no score")
    record = {k: verdict[k] for k in ("rating", "score_kind", "messages", "interrupts", "inserted", "replies")
              if k in verdict}
    if isinstance(verdict.get("usage"), dict):
        record["user_usage"] = verdict["usage"]
    return JudgeResult(float(verdict["score"]), _said(verdict), record)


def verified(task: dict, output: RunOutput) -> JudgeResult:
    """The task's own verifier's verdict (hermit.task.verify.environment), with the user's rating beside it."""
    _refused(output)
    result = environment(task, output)
    verdict = (output.extra or {}).get("user")
    if isinstance(verdict, dict) and not verdict.get("error") and verdict.get("score") is not None:
        result.extra.update({"user_score": float(verdict["score"]), "user_rating": verdict.get("rating")})
    return result
