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
# A number token: digits with optional thousands separators and decimals; a
# sentence-ending period after it does not hide it.
NUMBER = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?(?!\w|\.\d)")
NUMBER_WORDS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve "
                                           "thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty".split())}
SMALL_CONSTANTS = set(range(0, 13)) | {52, 100, 365, 1000}


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


def _value(token: str):
    try:
        return abs(float(token.replace(",", "").rstrip("%")))
    except ValueError:
        return None


def _significant(token: str) -> int:
    return len(token.replace(",", "").replace("-", "").replace("%", "").replace(".", "").strip("0"))


def numbers_in(text: str) -> list[tuple[str, float]]:
    return [(t, v) for t in NUMBER.findall(str(text)) for v in [_value(t)] if v is not None]


def big_numbers(text: str) -> list[str]:
    """Numbers above 400 with three or more significant digits that are not years or round thousands."""
    found = []
    for token, value in numbers_in(text):
        if value > 400 and _significant(token) >= 3 and not (1900 <= value <= 2100 and value.is_integer()):
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


def reference_leaks(card: dict, released: dict, support: list[int]) -> list[str]:
    """Card numbers or text that equal a released reference answer.

    For the card's own support questions any equal number (spelled out or not)
    is a leak; for other released questions, numbers with two or more
    significant digits outside a small allowlist of constants and years."""
    schema, _ = core()
    text = schema.public_text(card)
    values = {round(v, 6) for _, v in numbers_in(text)}
    values |= {float(NUMBER_WORDS[w]) for w in schema.normalize(text).split() if w in NUMBER_WORDS}
    normalized = " " + schema.normalize(text) + " "
    findings = []
    for ordinal, record in released.items():
        reference = str(record.get("reference", ""))
        for token, value in numbers_in(reference):
            if round(value, 6) not in values:
                continue
            if ordinal in support:
                findings.append(f"reference value of support q{ordinal:03}: {token}")
            elif _significant(token) >= 2 and value not in SMALL_CONSTANTS and not (1900 <= value <= 2100 and value.is_integer()):
                findings.append(f"reference value of q{ordinal:03}: {token}")
        words = schema.normalize(reference)
        if words and not NUMBER.fullmatch(reference.strip()) and len(words) >= 4 and any(c.isalpha() for c in words):
            if " " + words + " " in normalized:
                findings.append(f"reference text of q{ordinal:03}")
    return findings


def leak_findings(card: dict, released: dict, support: list[int] = ()) -> list[str]:
    schema, _ = core()
    text = schema.public_text(card)
    findings = [f"{label}: {m.group(0)}" for pattern, label in LEAK for m in [pattern.search(text)] if m]
    numbers = big_numbers(text)
    if numbers:
        findings.append("large number: " + ", ".join(numbers[:3]))
    gram = shared_run(text, [r["question"] for r in released.values()])
    if gram:
        findings.append(f"copies a question: '{gram}'")
    findings += reference_leaks(card, released, list(support))
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
    support = []
    try:
        support = schema.support_list(card.get("support"))
        outside = [o for o in support if o not in pool_ids]
        if outside:
            reasons.append("support outside the pool: " + ", ".join(map(str, outside)))
        if len(support) < SUPPORT_MIN:
            reasons.append(f"support needs at least {SUPPORT_MIN} pool question")
    except ValueError as exc:
        reasons.append(str(exc))
    reasons += ["leak - " + f for f in leak_findings(card, released, support)]
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
    A run lost to infrastructure is excluded and counts for nothing; a question
    with fewer than k valid runs is inconclusive and blocks admission without
    counting as a rejection. Every other status that produced no correct
    answer is the actor's own 0.
    """
    valid = [r for r in runs if not r.get("excluded")]
    support_q = sorted({r["q"] for r in runs if r["role"] == "support"})
    keep_q = sorted({r["q"] for r in runs if r["role"] == "keep"})
    inconclusive = [q for q in support_q + keep_q if sum(1 for r in valid if r["q"] == q) < k]
    support_runs = [r for r in valid if r["role"] == "support"]
    misses = sum(1 for r in support_runs if not r["strict"])
    # One support question must flip in every run; each further support question
    # buys one miss. Under a 0.26 chance flip the null pass rate stays near 5%.
    allowed = max(0, len(support_q) - 1)
    support_ok = not any(q in inconclusive for q in support_q) and bool(support_runs) and misses <= allowed
    lost = [q for q in keep_q if q not in inconclusive and all(not r["strict"] for r in valid if r["q"] == q)]
    keep_ok = not lost and not any(q in inconclusive for q in keep_q)
    known = [(r["out_tokens"], originals.get(r["q"])) for r in valid
             if isinstance(r.get("out_tokens"), int) and isinstance(originals.get(r["q"]), int) and originals.get(r["q"]) > 0]
    ratio = (sum(a for a, _ in known) / len(known)) / (sum(b for _, b in known) / len(known)) if known else None
    cost_ok = ratio is None or ratio <= COST_MAX
    reason = (f"support {len(support_runs) - misses}/{len(support_runs)} (allowed misses {allowed})"
              + f", keep lost {lost if lost else 'none'} of {keep_q}"
              + (f", inconclusive {inconclusive}" if inconclusive else "")
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


def next_id(state_next: int, deck: list[dict], ledger: list[dict]) -> int:
    """The next free card id number: never reuse an id the deck or the ledger has seen."""
    seen = [c["id"] for c in deck] + [row.get("id") for row in ledger] + [(row.get("card") or {}).get("id") for row in ledger]
    numbers = [int(i[1:]) for i in seen if isinstance(i, str) and re.fullmatch(r"C\d{3}", i)]
    return max([int(state_next or 1)] + [n + 1 for n in numbers])


def status_text(block: int, pool_ids: list[int], right_ids: list[int], released: dict, deck: list[dict],
                hits: dict, ledger: list[dict], layout: dict | None = None, stale: list[int] = (),
                b_max: int = B_MAX) -> str:
    """What the learner is told about the state of the deck and the pool."""
    schema, render = core()
    lines = [f"## Deck state before block {block} learning",
             f"You may propose at most B = {b_max} cards this block. Each needs support from at least {SUPPORT_MIN} pool question; "
             f"verification re-runs each support question {K} times and allows (support count - 1) misses in total, "
             f"so one support question must be right in every run.",
             f"Pool (released questions answered wrong under the current deck, unclaimed): {pool_ids or 'empty'}",
             f"Right (released questions answered correctly under the current deck; control questions are drawn from these): {right_ids or 'none'}"]
    if stale:
        lines.append(f"Older wrong questions answered under an earlier deck (evidence only; not eligible as support or control): {list(stale)}")
    lines.append("")
    for o in pool_ids:
        r = released[o]
        lines += [f"### pool q{o:03} - status {r.get('status')}",
                  "Question: " + r["question"][:600] + ("..." if len(r["question"]) > 600 else ""),
                  f"Submitted: {r['submitted'] or '(empty)'}", f"Reference: {r['reference']}", ""]
    admitted = render.admitted(deck)
    lines.append("## Admitted cards, in the order the actor reads them" if admitted else "## Admitted cards: none yet")
    for card in admitted:
        record = hits.get(card["id"], [])
        won = sum(1 for _, s in record if s)
        note = " (NOT rendered: deck byte budget)" if layout and card["id"] in layout.get("overflow", []) else ""
        lines += [f"- {card['id']} admitted in block {card.get('admitted')}; support q{schema.support_list(card.get('support'))}; "
                  f"later matched questions {won} right / {len(record) - won} wrong{note}",
                  schema.render(card).rstrip(), ""]
    if layout:
        lines.append(f"Deck bytes {layout.get('bytes')} of {render.CARDS_MAX}; a proposal that does not fit is refused.")
    lines.append("")
    lines.append("## Earlier proposals (ledger)" if ledger else "## Earlier proposals: none")
    for row in ledger[-12:]:
        card = row.get("card") or {}
        lines.append(f"- block {row['t']} {row.get('id') or card.get('id') or '-'}: {row['action']} - "
                     f"{'ADMITTED' if row.get('admitted') else 'not admitted'}; {row.get('reason', '')}")
        if card.get("claim"):
            lines.append(f"  scope: {card.get('scope')} | claim: {str(card.get('claim'))[:300]}"
                         + (f" | unless: {str(card.get('unless'))[:150]}" if card.get("unless") else ""))
        if row.get("rationale"):
            lines.append(f"  rationale: {str(row['rationale'])[:300]}")
    return "\n".join(lines) + "\n"
