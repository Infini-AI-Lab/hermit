"""The CL-Bench host over a Harness Adapter: a ContinualLearningSystem for clbench's runner, no learner inside.

CL-Bench owns the loop: it asks the system for one environment action per turn (`run_action_turn`) and executes it
on its own side. This host is that system with an adapter plugged in through `hermit.Session` and nothing
else: the adapter's context is the system prompt, computed once per instance from the instance's first prompt
(ADAPTER.md, plus what the adapter's context tool renders for that prompt when its manifest declares one, as the
ReHarness template's retrieved reflections; without one, ADAPTER.md alone) and kept for every turn of the instance,
the tool's whole result in `context_info` for the runner's record; its tools are offered beside the task's, a call
to one of them runs through the Session. Ported from metaworld's host (mwm/seat.py and the clbench systems built on
it), reduced to the host side; sub-agents were already off in the framework's runs. Behaviour kept as it was:

- one bare chat call per environment action, the wm call cap per episode (100) with the same notice;
- every assistant reply recorded as turn records in the native shape (the venue's evidence writer groups them);
- the host's token usage per episode appended to <workspace>/cost.jsonl; poker also logs hands.jsonl / traj.jsonl.

This file is the same in both CL-Bench tasks; `venue` selects the two small differences in the records.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import time
from typing import Any, Optional

from src.interface import ContinualLearningSystem, Observation, Query, Response, observation_marks_instance_complete
from src.systems.utils import native_tools

from hermit import Session

logger = logging.getLogger(__name__)

# wm calls per episode, every venue: a runaway guard, not a budget (metaworld, 2026-09-13).
CALLS_PER_EPISODE = 100
CAP_NOTICE = f"wm call limit reached for this episode ({CALLS_PER_EPISODE}); act on the environment."
BIG_BLIND_CHIPS = 10
# Harness-internal metadata of the database venue that names a location on the operator's machine, or the
# dataset authors' difficulty label: never in a record the learner reads.
META_HIDDEN_METADATA = ("db_path", "difficulty")


def wm_text(result: Any) -> str:
    """A tool result as the host reads it; a paged `read` of the mwm template gets the way to the next page."""
    text = Session.text(result)
    if isinstance(result, dict) and "text" in result and result.get("eof") is False \
            and result.get("next_offset") is not None:
        followup = {"path": result.get("path"), "offset": result["next_offset"], "limit": result.get("limit", 4000)}
        if result.get("sha256"):
            followup["expected_sha256"] = result["sha256"]
        text += '\n[More text available; continue when useful: wm(name="read", args=' + json.dumps(followup) + ')]'
    return text


def wm_parts(name: str, args) -> tuple[str, dict]:
    """The operation a `wm(name=..., args=...)` call names, for the records and the uptake log; other tools as is."""
    args = args if isinstance(args, dict) else {}
    if name == "wm":
        inner = args.get("args")
        return str(args.get("name", "")), (inner if isinstance(inner, dict) else {})
    return name, args


class AdapterHost(ContinualLearningSystem):
    supports_baseline = False
    parallel_safe = False

    def __init__(self, session: Session, *, model: str, workspace: Path, venue: str, name: str = "harness_adapter"):
        if venue not in ("database", "poker"):
            raise ValueError(f"venue must be database or poker, not {venue!r}")
        self.session, self.model, self.venue, self._name = session, model, venue, name
        ws = Path(workspace)
        ws.mkdir(parents=True, exist_ok=True)
        self.cost_log, self.uptake_log = ws / "cost.jsonl", ws / "uptake.jsonl"
        self.hand_log, self.traj_log = ws / "hands.jsonl", ws / "traj.jsonl"
        self._messages: list[dict] = []
        self._turns: list[dict] = []          # this episode's records, in the official-trace shape
        self._pending: list[dict] = []        # finished episodes' records (the evidence writer reads them)
        self._current_instance: Optional[str] = None
        self._interaction_count = 0
        self._done = 0
        self._tokens = {"input": 0, "cached": 0, "output": 0}
        self._pending_call_id = None          # the task tool call awaiting the environment
        self._wm_calls = 0
        self._context: Optional[str] = None   # the adapter's context for the current instance (the system message)
        self.context_info: Optional[dict] = None   # the context tool's whole result for it; None without such a tool

    # ----------------------------------------------------------------------------------------------- one turn
    def respond(self, query: Query) -> Response:
        self._interaction_count += 1
        if query.instance_id != self._current_instance:
            self._current_instance = query.instance_id
            self._messages = []
            self._wm_calls = 0
            self._pending_call_id = None
            # The adapter's context for this instance, once: ADAPTER.md, and below it what the adapter's context tool
            # renders for the instance's first prompt (x_t) when its manifest declares one; an adapter without one
            # gets ADAPTER.md alone, and `last_context` stays None.
            self._context = self.session.context(query=query.prompt)
            self.context_info = self.session.last_context
        self._messages.append({"role": "user", "content": query.prompt if query.prompt else "(no content)"})
        try:
            turn = self._turn(query)
        except Exception as exc:                       # noqa: BLE001
            logging.warning("host call exhausted retries (%s); cooling down 120s for one final attempt", exc)
            time.sleep(120)
            turn = self._turn(query)
        self._pending_call_id = turn.pending_call_id
        return Response(action=turn.action, metadata={
            "system_type": "harness_adapter" if self.venue == "database" else "harness_adapter_poker",
            "architecture": "mwm", "model": self.model,
            "interaction_count": self._interaction_count, "channel_actions": self._wm_calls})

    def _turn(self, query: Query):
        """Drive the model to one environment action; the adapter's tools are answered inside the turn."""
        calls_of_reply: list = []

        def _reply(msg, reasoning):
            self._record_turn(query, msg, reasoning)
            calls_of_reply[:] = list(msg.get("tool_calls") or [])

        turn = native_tools.run_action_turn(
            self.model, [{"role": "system", "content": self._context}], self._messages,
            list(query.tools or []), query.response_schema,
            extra_tools=self.session.tool_specs("openai"), extra_handler=self._adapter_tool,
            on_usage=self._on_usage, on_reply=_reply)
        self._fill_answered(turn.answered)
        return turn

    def _adapter_tool(self, name: str, args) -> str:
        """One call to an adapter tool, as the model made it."""
        if self._wm_calls >= CALLS_PER_EPISODE:
            return CAP_NOTICE
        self._wm_calls += 1
        operation, operation_args = wm_parts(name, args)
        self._log_uptake(operation.upper() or name.upper(), json.dumps(operation_args)[:200])
        try:
            return wm_text(self.session.call(name, args if isinstance(args, dict) else {}))
        except Exception as exc:                        # noqa: BLE001 — a tool error is an observation
            return f"Error: {type(exc).__name__}: {exc}"

    def observe(self, observation: Observation, next_query: Optional[Query] = None) -> None:
        content = (observation.content or "").strip()
        target = self._turn_awaiting(self._pending_call_id)
        if target is not None:
            target["observation"] = {"content": content}
        if self._pending_call_id is not None:
            self._messages.append(native_tools.tool_result_message(self._pending_call_id, content))
            self._pending_call_id = None
        elif content and not observation_marks_instance_complete(observation):
            self._messages.append({"role": "user", "content": f"FEEDBACK: {content}"})
        if observation_marks_instance_complete(observation):
            self._done += 1
            if self.venue == "poker":
                self._log_hand(self._done, content)
                self._append(self.traj_log, {"t": time.time(), "hand": self._done,
                                             "instance_id": getattr(observation, "instance_id", None)
                                             or (observation.metadata or {}).get("instance_id"),
                                             "turns": self._turns})
            self._log_cost("host", self._done, dict(self._tokens))
            self._tokens = {"input": 0, "cached": 0, "output": 0}
            self._pending.extend(self._turns)
            self._turns = []
            self._messages = []
            self._current_instance = None

    # ----------------------------------------------------------------------------------------------- records
    def _record_turn(self, query: Query, reply: dict, reasoning: str | None = None) -> None:
        """One assistant reply as turn records: each tool call is a turn (adapter calls flagged channel=True with
        {name, args}; task tools as {action, ...}); a reply with no call is a turn whose action is its text."""
        meta = dict(query.metadata or {})
        if self.venue == "database":
            meta = {k: v for k, v in meta.items() if k not in META_HIDDEN_METADATA}
        base = {"query": {"prompt": query.prompt, "instance_id": query.instance_id, "metadata": meta},
                "reasoning": reasoning, "observation": None}
        calls = reply.get("tool_calls") or []
        text = reply.get("content") or ""
        if not calls:
            action = {"action": "TEXT", "content": text} if self.venue == "database" else {"action": "TEXT", "thinking": text}
            self._turns.append({**base, "channel": False, "response": {"action": action}})
            return
        for c in calls:
            name = c["function"]["name"]
            args = native_tools.parse_arguments(c["function"]["arguments"])
            if name in self.session.tool_names:
                operation, operation_args = wm_parts(name, args)
                self._turns.append({**base, "channel": True, "call_id": c["id"],
                                    "response": {"action": {"name": operation, "args": operation_args}}})
            elif self.venue == "database":
                self._turns.append({**base, "channel": False, "call_id": c["id"],
                                    "response": {"action": {"action": name, **args, **({"thinking": text} if text else {})}}})
            else:
                self._turns.append({**base, "channel": False, "call_id": c["id"],
                                    "response": {"action": {"thinking": text, "action": name, **args}}})

    def _fill_answered(self, answered: dict) -> None:
        """Every call answered inside the turn gets that answer as its turn's observation, matched by call id."""
        for t in self._turns:
            if t.get("observation") is None and t.get("call_id") in answered:
                t["observation"] = {"content": answered[t["call_id"]]}

    def _turn_awaiting(self, call_id):
        """The turn the environment's response belongs to: the pending call's turn, else the last task turn
        still without an observation."""
        if call_id is not None:
            for t in reversed(self._turns):
                if t.get("call_id") == call_id and t.get("observation") is None:
                    return t
        for t in reversed(self._turns):
            if not t.get("channel") and t.get("observation") is None:
                return t
        return None

    # ----------------------------------------------------------------------------------------------- ledgers
    def _on_usage(self, u) -> None:
        self.record_usage_event(u)
        self._tokens["input"] += u.input_tokens or 0
        self._tokens["output"] += u.output_tokens or 0
        self._tokens["cached"] += getattr(u, "cached_input_tokens", None) or 0

    @staticmethod
    def _append(path: Path, record: dict) -> None:
        try:
            with open(path, "a") as f:
                f.write(json.dumps(record) + "\n")
        except (OSError, TypeError):
            pass

    def _log_cost(self, role: str, round_no: int, tokens: dict) -> None:
        pin = float(os.environ.get("MW_COST_IN_PER_M", "0") or 0)
        pout = float(os.environ.get("MW_COST_OUT_PER_M", "0") or 0)
        self._append(self.cost_log, {"t": time.time(), "role": role, "round": round_no,
                                     "input_tokens": tokens["input"], "cached_input_tokens": tokens.get("cached", 0),
                                     "output_tokens": tokens["output"],
                                     "cost_usd": round(tokens["input"] / 1e6 * pin + tokens["output"] / 1e6 * pout, 6)})

    def _log_uptake(self, kind: str, content: str) -> None:
        self._append(self.uptake_log, {"t": time.time(), "kind": kind, "content": content[:200]})

    _NET = re.compile(r"Net chip change this hand: ([+-]?\d+) chips")

    def _log_hand(self, hand_no: int, content: str) -> None:
        m = self._NET.search(content or "")
        if m:
            chips = int(m.group(1))
            self._append(self.hand_log, {"t": time.time(), "hand": hand_no, "chips": chips, "bb": chips / BIG_BLIND_CHIPS})

    # ----------------------------------------------------------------------------------------------- protocol
    def reset(self) -> None:
        self._messages, self._turns, self._current_instance = [], [], None
        self._context, self.context_info = None, None
        self._tokens = {"input": 0, "cached": 0, "output": 0}

    @property
    def name(self) -> str:
        return self._name

    def get_run_artifacts(self) -> dict:
        return {"artifact_type": "harness_adapter", "adapter": str(self.session.root), "episodes_done": self._done}
