"""Block algorithm of the cards evolver: what may enter the deck, and why.

Pure functions over a run's released records. The host runs actors and grades
attempts; this module screens proposals, picks control questions, judges the
verification runs and assembles the next deck. The constants are protocol
parameters and are not part of any adapter.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
from pathlib import Path
import re
import shutil
import sys

B_MAX = 2          # proposals verified per block
K = 2              # re-runs per verification question
SUPPORT_MIN = 1    # pool questions a card must be written for; misses allowed = support count - 1
KEEP = 2           # control questions the controller adds
COST_MAX = 3.0     # mean verification output tokens / mean original output tokens
NGRAM = 8          # words a card may not share verbatim with a released question

LEAK = (
    (re.compile(r"\bUID\d{4}\b"), "question id"),
    (re.compile(r"\bq\d{3}\b"), "question ordinal"),
    (re.compile(r"\b(19|20)\d{2}_\d{2}\b"), "issue file stamp"),
    (re.compile(r"treasury_bulletin_\S+"), "corpus file name"),
    (re.compile(r"\S+\.txt\b"), "file name"),
)
NUMBER = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?(?![\w.])")


def core():
    """The architecture's own schema and renderer, loaded from the framework copy."""
    root = Path(__file__).resolve().parents[2] / "architectures" / "cards" / "cards_core"
    name = "_cards_core_evolver"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, root / "__init__.py", submodule_search_locations=[str(root)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return importlib.import_module(name + ".schema"), importlib.import_module(name + ".render")


def big_numbers(text: str) -> list[str]:
    """Numbers with three or more significant digits above 400 that are not years."""
    found = []
    for token in NUMBER.findall(text):
        digits = token.replace(",", "").replace("-", "")
        try:
            value = abs(float(digits))
        except ValueError:
            continue
        significant = len(digits.replace(".", "").lstrip("0"))
        if value > 400 and significant >= 3 and not (1900 <= value <= 2100 and float(value).is_integer()):
            found.append(token)
    return found


def shared_run(text: str, questions: list[str], n: int = NGRAM) -> str | None:
    schema, _ = core()
    words = schema.normalize(text).split()
    grams = {" ".join(words[i:i + n]) for i in range(len(words) - n + 1)}
    for question in questions:
        q = schema.normalize(question).split()
        for i in range(len(q) - n + 1):
            gram = " ".join(q[i:i + n])
            if gram in grams:
                return gram
    return None


def leak_findings(card: dict, questions: list[str]) -> list[str]:
    schema, _ = core()
    text = schema.public_text(card)
    findings = [f"{label}: {m.group(0)}" for pattern, label in LEAK for m in [pattern.search(text)] if m]
    numbers = big_numbers(text)
    if numbers:
        findings.append("large number: " + ", ".join(numbers[:3]))
    gram = shared_run(text, questions)
    if gram:
        findings.append(f"copies a question: '{gram}'")
    return findings


def card_sha(card: dict) -> str:
    schema, _ = core()
    return hashlib.sha256(schema.normalize(schema.public_text(card)).encode()).hexdigest()


def pool(released: dict, claimed: dict, excluded=()) -> list[int]:
    """Released questions the current deck got wrong, unclaimed, not transport-lost."""
    return sorted(o for o, r in released.items()
                  if not r["strict"] and not r.get("transport_failed") and str(o) not in claimed and o not in excluded)


def right(released: dict, excluded=()) -> list[int]:
    return sorted(o for o, r in released.items() if r["strict"] and o not in excluded)


def screen(card: dict, pool_ids: list[int], released: dict, rejected: list[str], existing_ids: list[str]) -> dict:
    """Schema, support, leak and duplicate checks; nothing here runs a model."""
    schema, _ = core()
    reasons = list(schema.validate(card))
    if card.get("admitted") is not None:
        reasons.append("admitted is written by the controller only")
    if card.get("id") in existing_ids:
        reasons.append("id already exists in the deck")
    try:
        support = schema.support_list(card.get("support"))
        outside = [o for o in support if o not in pool_ids]
        if outside:
            reasons.append("support outside the pool: " + ", ".join(map(str, outside)))
        if len(support) < SUPPORT_MIN:
            reasons.append(f"support needs at least {SUPPORT_MIN} pool questions")
    except ValueError as exc:
        reasons.append(str(exc))
    reasons += ["leak - " + f for f in leak_findings(card, [r["question"] for r in released.values()])]
    sha = card_sha(card)
    if sha in rejected:
        reasons.append("identical to a card already rejected")
    return {"ok": not reasons, "reasons": reasons, "sha": sha}


def choose_keep(right_ids: list[int], scope: str, released: dict, exclude: list[int], n: int = KEEP) -> list[int]:
    """Control questions: prefer ones the scope matches, then the most recent; never the support."""
    schema, _ = core()
    candidates = [o for o in right_ids if o not in exclude]
    matched = sorted((o for o in candidates if schema.matches(released[o]["question"], scope)), reverse=True)
    rest = sorted((o for o in candidates if o not in matched), reverse=True)
    return (matched + rest)[:n]


def judge(runs: list[dict], originals: dict, k: int = K) -> dict:
    """Admission from verification runs.

    runs: {"q", "role": "support"|"keep", "k", "strict", "status", "out_tokens", "excluded"}.
    originals: q -> output tokens of the original scored attempt (None when unknown).
    A transport-lost run is excluded and counts for nothing; every other status
    that produced no correct answer is the actor's own 0.
    """
    valid = [r for r in runs if not r.get("excluded")]
    support_q = sorted({r["q"] for r in runs if r["role"] == "support"})
    keep_q = sorted({r["q"] for r in runs if r["role"] == "keep"})
    inconclusive = [q for q in support_q if not any(r["q"] == q for r in valid)]
    support_runs = [r for r in valid if r["role"] == "support"]
    misses = sum(1 for r in support_runs if not r["strict"])
    # One support question must flip in every run; each further support question
    # buys one miss. Under a 0.26 chance flip the null pass rate stays near 5%.
    allowed = max(0, len(support_q) - 1)
    support_ok = not inconclusive and bool(support_runs) and misses <= allowed
    lost = []
    for q in keep_q:
        mine = [r for r in valid if r["q"] == q]
        if len(mine) >= 2 and all(not r["strict"] for r in mine):
            lost.append(q)
    keep_ok = not lost
    known = [(r["out_tokens"], originals.get(r["q"])) for r in valid
             if isinstance(r.get("out_tokens"), int) and isinstance(originals.get(r["q"]), int) and originals.get(r["q"]) > 0]
    ratio = (sum(a for a, _ in known) / len(known)) / (sum(b for _, b in known) / len(known)) if known else None
    cost_ok = ratio is None or ratio <= COST_MAX
    reason = (f"support {len(support_runs) - misses}/{len(support_runs)} (allowed misses {allowed})"
              + (f" (inconclusive: {inconclusive})" if inconclusive else "")
              + f", keep lost {lost if lost else 'none'} of {keep_q}"
              + (f", cost x{ratio:.2f}" if ratio is not None else ", cost unknown"))
    return {"admitted": support_ok and keep_ok and cost_ok, "support_ok": support_ok, "keep_ok": keep_ok,
            "cost_ok": cost_ok, "misses": misses, "inconclusive": inconclusive, "lost": lost,
            "tokens_ratio": ratio, "reason": reason}


def assemble(source: Path, target: Path, cards: list[dict], block: int, after_copy=None) -> list[str]:
    """Copy the current deck and add the admitted cards, marked with this block.

    after_copy(target), when given, runs between the copy and the card writes;
    a host uses it to start the revision history on the untouched copy."""
    schema, _ = core()
    source, target = Path(source), Path(target)
    if target.exists():
        raise FileExistsError(target)
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    if after_copy is not None:
        after_copy(target)
    written = []
    for card in cards:
        path = target / "cards" / f"{card['id']}.md"
        if path.exists():
            raise FileExistsError(path)
        record = {k: card[k] for k in ("id", "scope", "claim", "unless", "support") if k in card}
        record["admitted"] = block
        errors = schema.validate(record)
        if errors:
            raise ValueError(f"{card['id']}: " + "; ".join(errors))
        path.write_text(schema.dumps(record))
        written.append(path.name)
    return written


def status_text(block: int, pool_ids: list[int], right_ids: list[int], released: dict, deck: list[dict],
                hits: dict, ledger: list[dict], b_max: int = B_MAX) -> str:
    """What the learner is told about the state of the deck and the pool."""
    schema, _ = core()
    lines = [f"## Deck state before block {block} learning",
             f"You may propose at most B = {b_max} cards this block. Each needs support from at least {SUPPORT_MIN} pool question; "
             f"verification re-runs each support question {K} times and allows (support count - 1) misses in total, "
             f"so one support question must be right in every run.",
             f"Pool (released questions answered wrong under the current deck, unclaimed): {pool_ids or 'empty'}",
             f"Right (released questions answered correctly; control questions are drawn from these): {right_ids or 'none'}", ""]
    for o in pool_ids:
        r = released[o]
        lines += [f"### pool q{o:03} - status {r.get('status')}",
                  "Question: " + r["question"][:600] + ("..." if len(r["question"]) > 600 else ""),
                  f"Submitted: {r['submitted'] or '(empty)'}", f"Reference: {r['reference']}", ""]
    lines.append("## Admitted cards" if deck else "## Admitted cards: none yet")
    for card in deck:
        record = hits.get(card["id"], [])
        won = sum(1 for _, s in record if s)
        lines.append(f"- {card['id']} (block {card.get('admitted')}; scope: {', '.join(schema.keywords(card['scope']))}; "
                     f"support q{card.get('support')}; later matched questions {won} right / {len(record) - won} wrong)")
    lines.append("")
    lines.append("## Earlier proposals (ledger)" if ledger else "## Earlier proposals: none")
    for row in ledger[-12:]:
        lines.append(f"- block {row['t']} {row.get('id', '-')}: {row['action']} - {'ADMITTED' if row.get('admitted') else 'not admitted'}; {row.get('reason', '')}")
    return "\n".join(lines) + "\n"
