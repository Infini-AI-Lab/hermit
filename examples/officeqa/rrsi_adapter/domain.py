"""OfficeQA as an RRSI Adapter domain: the texts (texts/: the constitution and the four roles' briefs, adapted from
upstream's coding and workspace domains), the critic's patterns, the answers the judge's feedback reveals (added to
the critic's denylist as they are seen), and the smoke tasks."""
from __future__ import annotations

import json
from pathlib import Path
import re

import yaml

from hermit.adapter.manifest import read_exports
from hermit.agent.evolver.methods.rrsi_adapter.domain import AdapterDomain

HERE = Path(__file__).resolve().parent
EXPECTED = re.compile(r"but expected '([^']*)'")
HELD = "the plan held the session back"           # the Codex Base Agent's words for a session the plan never ran


def _held(record: dict) -> bool:
    """A rollout the ChatGPT plan never ran: the acting agent gave up after the plan held its session back past
    plan_max_wait_s (the pipeline's error, which the judge's feedback carries)."""
    text = str(record.get("error") or "") + str((record.get("judge") or {}).get("feedback") or "")
    return record.get("status") == "error" and HELD in text


class OfficeQADomain(AdapterDomain):
    name = "officeqa"
    texts = HERE / "texts"
    # The components the reported runs could carry: they deployed the adapter with #54's Codex agent, which connected
    # no hooks and no roles, so control_flow, output_plumbing, context_mgmt and subagent had no carrier and no
    # reserved slot asked for them. The framework's Codex agent now connects what an exports.yaml declares; the
    # slot keeps the measured set until a run measures the rest.
    carriers = ["prompt", "config", "client_tool", "skill", "memory"]
    critic_patterns = [
        (r"\bUID\d{4}\b", "question id in diff"),
        (r"treasury_bulletin_\d{4}_\d{2}", "a specific bulletin file name in diff"),
        (r"answers\.jsonl|/\.private/|utils/judge\.py|skillopt\.envs\.officeqa|normalize_answer|exact_match",
         "answers, judge or scoring code referenced in diff"),
    ]

    def screen(self, adapter: Path) -> list[str]:
        """Python an exports.yaml names (an `entrypoint` anywhere in it: a tool, a context tool, an exporter, a
        reaction) runs on the framework's side, outside the agent's container, where the answers are; what the agent
        runs itself stays in its container. Read as the framework reads the file (YAML, quoting and merges resolved),
        not as a pattern over the diff."""
        path = Path(adapter) / "exports.yaml"
        if path.is_symlink() or (path.exists() and not path.is_file()):
            return ["exports.yaml must be a file"]
        try:
            data = read_exports(Path(adapter))             # the framework's own reading of the file
        except (ValueError, yaml.YAMLError, UnicodeDecodeError) as exc:
            return [f"exports.yaml does not read as exports: {str(exc)[:200]}"]
        found = []

        def walk(node, where: str) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    at = f"{where}.{key}" if where else str(key)
                    named = str(key) in ("entrypoint", "context_tool", "reactions") or \
                        (not where and str(key) == "tools")
                    if named and value not in (None, "", [], {}):          # declared empty: names nothing
                        found.append(at)
                    walk(value, at)
            elif isinstance(node, list):
                for i, value in enumerate(node):
                    walk(value, f"{where}[{i}]")
        try:
            walk(data, "")
        except RecursionError:                              # a YAML alias that holds itself
            return ["exports.yaml refers to itself (a recursive YAML alias)"]
        return [f"exports.yaml names Python the framework would run outside the agent's container: {found[:5]}"] \
            if found else []

    def leak_patterns(self, records: list[dict]) -> list:
        """The training answers the judge's feedback has revealed, as patterns over the diff's added lines: each
        whole answer of four or more characters with a digit, and each number inside an answer with three or more
        significant digits (thousands separators optional), so an answer copied into the adapter, whole or in part,
        is rejected before measurement. Bare years, short numbers and round numbers (a whole answer too) are left to
        the LLM critic: they occur in general procedure too. The reason names the value (the proposer wrote it;
        nothing new is revealed)."""
        answers = set()
        for record in records:
            m = EXPECTED.search(str((record.get("judge") or {}).get("feedback") or ""))
            if m:
                answers.add(m.group(1).strip())
        out, seen, numbers = [], set(), set()
        for value in sorted(answers):
            bare = value.replace(",", "")
            round_number = re.fullmatch(r"\d+(?:\.\d+)?", bare) and len(re.sub(r"\D", "", bare).strip("0")) < 3
            if len(value) >= 4 and re.search(r"\d", value) and not re.fullmatch(r"(19|20)\d\d", value) \
                    and not round_number and value not in seen:
                seen.add(value)
                out.append((rf"(?m)^\+.*(?<![\w.]){re.escape(value)}(?![\w.])",
                            f"a training answer in diff: {value!r}"))
            for number in re.findall(r"\d[\d,]*\.?\d*", value):
                digits = re.sub(r"\D", "", number).strip("0")       # significant digits, round numbers short
                plain = number.replace(",", "").rstrip(".")
                if len(digits) < 3 or re.fullmatch(r"(19|20)\d\d", plain) or plain in numbers:
                    continue
                numbers.add(plain)
                whole, _, frac = plain.partition(".")
                grouped = re.sub(r"\B(?=(\d{3})+$)", ",?", whole)
                pattern = grouped + (r"\." + re.escape(frac) if frac else "")
                out.append((rf"(?m)^\+.*(?<![\d.,]){pattern}(?![\d])", f"a number from a training answer in diff: "
                                                                          f"{plain!r}"))
        return out

    def halted_rows(self, material: Path) -> list[str]:
        """Rows the plan never ran: their evaluation is not the candidate's, so it is measured again."""
        out = []
        for path in sorted((Path(material) / "tasks").glob("*.json")):
            try:
                record = json.loads(path.read_text())
            except ValueError:
                continue
            if isinstance(record, dict) and _held(record):
                out.append(str(record.get("id")))
        return out

    def halted_result(self, result: dict) -> bool:
        """A smoke rollout the plan never ran: no verdict on the candidate (the step stops; the smoke runs later)."""
        return _held(result)

    def smoke_failure(self, result: dict) -> bool:
        """Upstream fails its smoke on a trial that ended with an exception, timeouts included: here a rollout that
        timed out or whose CLI exited non-zero, as the Trainer's `run_agent` reports them (`timed_out`,
        `exit_code`). A wrong answer, or a session that ended without an `<answer>`, passes. Where `run_agent` does
        not report them, a rollout without an answer is taken for one that did not finish (the smoke's questions
        are ones the incumbent answered right in every trial)."""
        if super().smoke_failure(result):
            return True
        if "timed_out" in result or "exit_code" in result:
            return bool(result.get("timed_out")) or bool(result.get("exit_code"))
        out = result.get("output") if isinstance(result.get("output"), dict) else {}
        return not str(out.get("answer") or "").strip()

    def smoke_ids(self, incumbent_per_task: dict | None = None) -> list[str]:
        """Upstream's coding smoke runs two fixed tasks once each; here the first recorded row of the first two
        questions the incumbent answered right in every trial (else of the first two questions with a recorded row:
        a question all of whose trials are missing has no row to run)."""
        per_task = incumbent_per_task or {}
        recorded = {t: [r for r in (tr.extra or {}).get("rows") or [] if r] for t, tr in per_task.items()}
        solved = [t for t, tr in sorted(per_task.items()) if tr.rewards and min(tr.rewards) >= 1.0 and recorded[t]]
        chosen = (solved or [t for t in sorted(per_task) if recorded[t]])[:2]
        return [recorded[t][0] for t in chosen]
