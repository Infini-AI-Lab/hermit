"""k trials of a training question as k rows: `make_rows.py` writes `<id>#<k>` rows; the acting agent and the judge
here see the question's own id, so a trial row runs and scores exactly as the question does (its session's directory
is named after the question too)."""
from __future__ import annotations

from pathlib import Path
import re

from hermit.agent.acting.workspace import CliAgent
from hermit.common import load_callable

HERE = Path(__file__).resolve().parent
UTILS = HERE.parent / "utils"


def question(row_id: str) -> str:
    return row_id.rsplit("#", 1)[0] if re.fullmatch(r".+#\d+", row_id) else row_id


class TrialCodexAgent(CliAgent):
    """`agent.name: trials.py:TrialCodexAgent` with `agent.base: codex`: the framework's Codex agent (the class
    `agent.name: codex` builds, hermit/agent/acting/workspace.py) on a row's question."""

    def run(self, task: dict, adapter):
        return super().run({**task, "id": question(task["id"])}, adapter)


_JUDGE: list = []                                    # the score module, loaded once (its answers with it)


def score(task: dict, output) -> dict:
    """`judge.path: trials.py`: SkillOpt's exact match (../utils/benchmark/judge.py) on the row's question."""
    if not _JUDGE:
        _JUDGE.append(load_callable(str(UTILS / "benchmark" / "judge.py") + ":score", UTILS))
    return _JUDGE[0]({**task, "id": question(task["id"])}, output)
