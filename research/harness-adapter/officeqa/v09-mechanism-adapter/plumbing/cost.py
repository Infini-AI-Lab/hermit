"""Read-only Fireworks usage ledger; writes an estimate only when explicitly asked.

No inference requests, answer files, prompts, credentials, or model output are used.
Per-call transport attempts are counted once; session summaries are never added.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile

MODEL = "accounts/fireworks/models/glm-5p3-flash"
PRICE_DATE = "2026-09-21"
PRICE_URLS = ["https://fireworks.ai/models/fireworks/glm-5p3-flash",
              "https://docs.fireworks.ai/serverless/pricing"]
RATES = {"uncached_input": Decimal("0.15"), "cached_input": Decimal("0.03"),
         "output": Decimal("0.50")}
MILLION = Decimal(1_000_000)


def read(path):
    return json.loads(Path(path).read_text())


def count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def money(input_tokens, cached_tokens, output_tokens):
    """Exact decimal arithmetic, converted only for JSON serialization."""
    return float(((input_tokens - cached_tokens) * RATES["uncached_input"]
                  + cached_tokens * RATES["cached_input"]
                  + output_tokens * RATES["output"]) / MILLION)


def usage_item(record, source):
    """Extract usage only. Absent usage is unknown, except a recorded non-dispatch."""
    no_dispatch = record.get("dispatched") is False or record.get("wire_dispatched") is False
    if no_dispatch:
        return {"source": source, "wire_dispatched": False, "complete": True,
                "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0,
                "known_cost_lower_bound_usd": 0.0, "estimate_usd": 0.0,
                "unknown_fields": []}
    usage = record.get("usage") or {}
    inp = count(usage.get("input_tokens", usage.get("prompt_tokens")))
    out = count(usage.get("output_tokens", usage.get("completion_tokens")))
    details = usage.get("input_tokens_details", usage.get("prompt_tokens_details")) or {}
    cached = count(details.get("cached_tokens", usage.get("cached_tokens")))
    if inp == 0 and cached is None:
        cached = 0
    unknown = [name for name, value in (("input_tokens", inp), ("output_tokens", out),
                                        ("cached_input_tokens", cached)) if value is None]
    if cached is not None and inp is not None and cached > inp:
        cached = None
        unknown.append("cached_input_tokens_exceed_input")
    if record.get("usage_unknown") is True or record.get("usage_complete") is False:
        unknown.append("provider_or_transport_usage_incomplete")
    # If cache breakdown is absent, price known input at the cheapest valid
    # rate for the lower bound. Never silently price unknown tokens as zero.
    lower_cache = cached if cached is not None and inp is not None else (inp or 0)
    lower = money(inp or 0, lower_cache if inp is not None else 0, out or 0)
    return {"source": source, "wire_dispatched": True, "complete": not unknown,
            "input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out,
            "known_cost_lower_bound_usd": lower,
            "estimate_usd": lower if not unknown else None,
            "unknown_fields": unknown,
            "response_id": record.get("response_id"),
            "response_model": record.get("response_model"),
            "status": record.get("status", record.get("response_status"))}


def actor_call(directory, root):
    """Use one logical transport's attempts, or persisted partial attempts. v9: the call's transport
    origin ('codex' or 'controller'; a missing value is a v8/controller=None record, i.e. 'codex')."""
    path = directory / "transport.json"
    record = read(path) if path.exists() else None
    if record is not None and record.get("attempts"):
        entries = [usage_item(a, str(path.relative_to(root)) + f"#attempt={i}")
                   for i, a in enumerate(record["attempts"], 1)]
    else:
        partial = sorted((directory / "attempts").glob("*/transport.json"))
        if partial:
            entries = [usage_item(read(p), str(p.relative_to(root))) for p in partial]
        elif record is not None and record.get("usage"):
            entries = [usage_item(record, str(path.relative_to(root)))]
        else:
            # A request exists without a completed transport record. It may be
            # queued or in flight; we cannot invent a billed amount.
            entries = [usage_item({}, str(directory.relative_to(root)) + "#pending")]
    origin = (record or {}).get("origin") or "codex"
    return {"call": str(directory.relative_to(root)), "origin": origin, "attempts": entries}


def api_calls(base, root):
    """Every api/NNN call directory with a request under `base` (any depth), as actor_call records."""
    calls = []
    for request in sorted(Path(base).glob("**/api/[0-9][0-9][0-9]/request.json")):
        calls.append(actor_call(request.parent, root))
    return calls


def learner_calls(control, root):
    result = []
    recorded = set()
    for path in sorted((control / "fireworks-calls").glob("*.json")):
        if not path.stem.isdigit():
            continue
        recorded.add(int(path.stem))
        record = read(path)
        meta = record.get("meta") or {}
        attempts = meta.get("attempts")
        if attempts:
            entries = [usage_item(a, str(path.relative_to(root)) + f"#attempt={i}")
                       for i, a in enumerate(attempts, 1)]
        else:
            entries = [usage_item(meta, str(path.relative_to(root)))]
        result.append({"call": str(path.relative_to(root)), "attempts": entries})
    state_file = control / "fireworks-result.json"
    if state_file.exists():
        pending = read(state_file).get("in_flight_request")
        if isinstance(pending, int) and pending not in recorded:
            result.append({"call": str(control.relative_to(root)) + f"/pending-{pending}",
                           "attempts": [usage_item({}, str(state_file.relative_to(root)) +
                                                   f"#in_flight_request={pending}")]})
    return result


def totals(calls):
    entries = [e for c in calls for e in c["attempts"]]
    unknown = [e for e in entries if not e["complete"]]
    values = {field: sum(e[field] for e in entries if e[field] is not None)
              for field in ("input_tokens", "cached_input_tokens", "output_tokens")}
    lower = sum((Decimal(str(e["known_cost_lower_bound_usd"])) for e in entries), Decimal(0))
    return {"logical_calls": len(calls),
            "wire_attempts": sum(e["wire_dispatched"] for e in entries),
            "known_usage": values,
            "known_cost_lower_bound_usd": float(lower),
            "estimate_usd": float(lower) if not unknown else None,
            "usage_complete": not unknown,
            "unknown_usage_attempts": len(unknown),
            "unknown_usage_logical_calls": sum(any(not e["complete"] for e in c["attempts"]) for c in calls),
            "unknown_records": [{"source": e["source"], "unknown_fields": e["unknown_fields"]} for e in unknown]}


def learner_controls(base):
    """Every learner session control directory (a directory holding fireworks-calls/) under `base`."""
    return sorted({p.parent for p in Path(base).glob("**/fireworks-calls") if p.is_dir()})


def estimate(root=None, *, include_ledger=False):
    """Return a live snapshot, without writing any file or modifying the run.

    v9 groups (one arm per root): scored actors split by transport origin (codex = the model calls
    Codex made; controller = broker enforcement re-queries, DESIGN §4), learner calls, verification
    actors (actor re-runs a learner requested, also split by origin), everything moved aside by a
    block-level resume (runs-aborted/: paid, never counted as formal results) and synthetic preflight."""
    root = Path(root or Path(__file__).parent).resolve()
    groups = {k: [] for k in ("actors_codex", "actors_controller", "learners", "verification_codex",
                              "verification_controller", "aborted_actors", "aborted_learners",
                              "synthetic_preflight")}
    for call in api_calls(root / "runs", root):
        groups["actors_controller" if call["origin"] == "controller" else "actors_codex"].append(call)
    evolution = root / "evolution"
    for call in api_calls(evolution, root):
        groups["verification_controller" if call["origin"] == "controller" else "verification_codex"].append(call)
    for control in learner_controls(evolution):
        groups["learners"].extend(learner_calls(control, root))
    aborted = root / "runs-aborted"
    groups["aborted_actors"].extend(api_calls(aborted, root))
    for control in learner_controls(aborted):
        groups["aborted_learners"].extend(learner_calls(control, root))
    # Only recorded synthetic probes in THIS root. Offline unit tests use temporary fake responses
    # and are deliberately outside the paid ledger.
    groups["synthetic_preflight"].extend(api_calls(root / "preflight", root))
    formal = sum((calls for group, calls in groups.items() if group != "synthetic_preflight"), [])
    all_calls = formal + groups["synthetic_preflight"]
    by_origin = {origin: totals([c for c in formal if c.get("origin", "codex") == origin and "origin" in c])
                 for origin in ("codex", "controller")}
    state = read(root / "state.json") if (root / "state.json").exists() else {}
    result = {"created_utc": datetime.now(timezone.utc).isoformat(),
              "kind": "public_standard_price_estimate_not_invoice",
              "model": MODEL, "price_checked_date": PRICE_DATE, "price_urls": PRICE_URLS,
              "price_usd_per_million_tokens": {k: float(v) for k, v in RATES.items()},
              "root": str(root), "campaign_status": state.get("status", "not_started"),
              "arm": (state.get("arm") or {}).get("name"),
              "groups": {k: totals(v) for k, v in groups.items()},
              "actor_calls_by_origin": by_origin,
              "formal_total": totals(formal), "including_synthetic_preflight": totals(all_calls),
              "counting_policy": "Count each logical call's transport attempts once, including failed calls; never add summaries or duplicated response copies. Controller re-queries are their own numbered calls (origin 'controller').",
              "scope": "Current root only. Learners include rejected updates and their verification actors; runs-aborted/ holds paid work redone after a block-level resume.",
              "limits": ["Estimate from recorded usage and 2026-09-21 public prices, not a provider invoice.",
                         "Missing usage or cache detail is unknown, never silently zero; known token charges are reported as a lower bound.",
                         "An unfinished campaign's snapshot excludes future calls and may change during file capture.",
                         "Excludes discounts, credits, taxes, local compute and prior experiments."]}
    if include_ledger:
        result["ledger"] = groups
    return result


def build(root=None):
    """Explicitly save only cost-estimate.json, atomically; run inputs untouched."""
    root = Path(root or Path(__file__).parent).resolve()
    output = root / "cost-estimate.json"
    value = estimate(root, include_ledger=True)
    with tempfile.NamedTemporaryFile("w", dir=root, prefix=".cost-", suffix=".json", delete=False) as f:
        json.dump(value, f, indent=2, ensure_ascii=False)
        f.write("\n")
        temp = Path(f.name)
    temp.replace(output)
    return output


def self_test():
    """Small invented fixtures; no corpus, answers, credentials, or network."""
    assert money(1_000_000, 200_000, 100_000) == .176
    missing = usage_item({"usage": {"input_tokens": 1_000_000, "output_tokens": 100_000}}, "fixture")
    assert missing["estimate_usd"] is None and missing["known_cost_lower_bound_usd"] == .08
    absent = usage_item({}, "fixture")
    assert absent["input_tokens"] is None and not absent["complete"]
    queued = usage_item({"dispatched": False, "phase": "waiting"}, "fixture")
    assert queued["complete"] and queued["estimate_usd"] == 0
    def save(p, value):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(value))
    with tempfile.TemporaryDirectory(prefix="officeqa-cost-fixture-") as tmp:
        r = Path(tmp)
        good = {"dispatched": True, "usage": {"input_tokens": 1_000_000, "output_tokens": 100_000,
                         "input_tokens_details": {"cached_tokens": 200_000}}}
        call = r / "runs/mech-evolve/001/api/001"
        save(call / "request.json", {})
        save(call / "transport.json", {"attempts": [good], "origin": "codex"})
        save(call / "attempts/01/transport.json", good)
        save(call / "response.json", good)
        save(call.parent / "summary.json", {"usage": good["usage"]})
        requery = r / "runs/mech-evolve/001/api/002"
        save(requery / "request.json", {})
        save(requery / "transport.json", {"attempts": [good], "origin": "controller", "parent": 1})
        control = r / "evolution/10/sessions/example/control"
        save(control / "fireworks-calls/01.json", {"meta": {"attempts": [good, {"dispatched": True, "usage_unknown": True}]}})
        save(control / "fireworks-result.json", {"in_flight_request": 2})
        x = estimate(r)
        assert x["groups"]["actors_codex"]["estimate_usd"] == .176
        assert x["groups"]["actors_controller"]["estimate_usd"] == .176
        assert x["actor_calls_by_origin"]["controller"]["logical_calls"] == 1
        assert x["formal_total"]["known_cost_lower_bound_usd"] == .528
        assert x["formal_total"]["unknown_usage_attempts"] == 2
        assert x["formal_total"]["estimate_usd"] is None
        assert x["groups"]["learners"]["logical_calls"] == 2
    return {"passed": True, "checks": 13, "paid_model_calls": 0}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=Path(__file__).parent)
    p.add_argument("--write", action="store_true", help="Save a ledger to root/cost-estimate.json")
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args()
    if args.self_test:
        print(json.dumps(self_test(), indent=2))
    elif args.write:
        print(build(args.root))
    else:
        print(json.dumps(estimate(args.root), indent=2, ensure_ascii=False))
