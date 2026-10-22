"""The evidence store of a ServeLearnBench desk (retail, banking): one file per served case.

Layout: /evidence/block<NNN>/e<index>.json holds one case's complete record — the instruction the host was given,
every step it took (its reasoning, the tool call, the observation), what it submitted, the reward. The review
prompt lists the block's cases, one entry each, with the record's address; there is no index file and no
wrapper-computed statistic. Test episodes are never recorded: the benchmark's methods never see them either.

Two omissions, on purpose: the window index (evaluator metadata, and the answer to the question this benchmark
asks — when did practice change) and the ground truth (a host never sees it). The learner reads what the host saw
and nothing more.

Ported from metaworld's mwm/venues/banking/evidence.py and the flywheelbench lane's record helpers.
"""
from __future__ import annotations

import json
from pathlib import Path

# Per tool observation, in characters, in the record the learner reads (the host itself sees the whole
# observation). The same cap as the other venues: it is a property of the method's channel, not of the venue.
OBS_CAP = 2000
INSTRUCTION_CHARS = 3000
VERDICT_CHARS = 3000

# The desk's decision tools, per domain: a verdict is a tool call, and `finish` carries the declaration.
DECISION_TOOLS = {
    "retail": ("cancel_pending_order", "place_order", "modify_pending_order_items",
               "return_delivered_order_items", "exchange_delivered_order_items"),
    "banking": ("approve_case", "deny_case", "escalate_case", "execute_leg", "decide_batch"),
}


def clip_obs(text, cap: int = OBS_CAP) -> str:
    s = text if isinstance(text, str) else str(text)
    if len(s) <= cap:
        return s
    return s[:cap] + f"\n\n[... {len(s) - cap} more characters truncated]"


def record_trace(trace: list) -> list:
    """Every step the host produced, with its reasoning; the step's `input` (which carries the served package) and
    the per-turn usage are left out."""
    out = []
    for s in trace or []:
        if not isinstance(s, dict):
            continue
        d = {k: s.get(k) for k in ("kind", "name", "args", "obs", "reasoning", "raw", "tool_calls",
                                   "invalid_reason", "finish_reason", "rejected") if s.get(k)}
        if s.get("kind") == "tool":
            d["obs"] = clip_obs(str(s.get("obs", "")))
        out.append(d)
    return out


def _terminal_text(step: dict, include_raw: bool = True) -> str:
    v = (step.get("args") or step.get("obs") or (step.get("raw") if include_raw else "") or "")
    return json.dumps(v, sort_keys=True) if isinstance(v, (dict, list)) else str(v)


def submitted(trace: list, domain: str) -> str:
    """What the host decided: its decision calls and its finish declaration, all of them."""
    decision = DECISION_TOOLS.get(domain, ())
    parts = ["; ".join(f"{s['name']}({json.dumps(s.get('args') or {}, sort_keys=True)})"
                       for s in (trace or []) if s.get("kind") == "tool" and s.get("name") in decision)]
    for s in reversed(trace or []):
        if s.get("kind") == "finish":
            payload = _terminal_text(s, include_raw=False).strip()
            if payload and payload not in ("{}", "[]"):
                parts.append(f"finish({payload})")
            break
    return " | ".join(p for p in parts if p)[:600]


def address(i: int, block: int) -> str:
    return f"/evidence/block{block:03d}/e{i:04d}.json"


def episode_record(i: int, t: int, block: int, instruction: str, reward: float, trace: list, domain: str) -> dict:
    return {"i": i, "t": t, "block": block, "instruction": instruction,
            "submitted": submitted(trace, domain), "reward": reward, "trace": record_trace(trace)}


def write_raw(evidence_dir: Path, ep: dict) -> str:
    """The record under block<NNN>/; returns its /evidence address."""
    bdir = Path(evidence_dir) / f"block{ep['block']:03d}"
    bdir.mkdir(parents=True, exist_ok=True)
    (bdir / f"e{ep['i']:04d}.json").write_text(json.dumps(ep, indent=1, ensure_ascii=False))
    return address(ep["i"], ep["block"])


def entry(ep: dict, raw_path: str) -> str:
    """One case in the block's listing: what it was, what the host submitted, what it scored, where the record is."""
    instr = " ".join((ep.get("instruction") or "").split())
    if len(instr) > INSTRUCTION_CHARS:
        instr = instr[:INSTRUCTION_CHARS] + " ..."
    sub = ep.get("submitted") or "(nothing submitted)"
    return "\n".join([f"## Case e{ep['i']} (t={ep.get('t')})", f"instruction: {instr}",
                      f"submitted: {sub[:VERDICT_CHARS]}", f"reward: {ep.get('reward')}", f"raw record: {raw_path}"])


def write_readme(evidence_dir: Path, env_evidence_text: str) -> None:
    """/evidence/README.md: the venue's own description of its evidence system (the contract's env_evidence slot)."""
    from . import readme
    readme.write(evidence_dir, env_evidence_text)
