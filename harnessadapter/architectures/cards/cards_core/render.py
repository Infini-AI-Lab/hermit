"""Compile the deck the acting agent reads.

The deck is the fixed preamble plus every admitted card, in a fixed order, cut
to a hard byte budget. The budget is enforced here, in code the actor's
adapter copy executes, so no host and no learner can serve more than this.
A card is never cut in the middle: when the budget runs out, the newest cards
are left out whole and reported as overflow.
"""
from __future__ import annotations

from pathlib import Path

from . import schema

CARDS_MAX = 16384   # bytes of compiled context, preamble included
EMPTY = "(No cards have been admitted yet.)\n"


def load(root: Path) -> list[dict]:
    """Every card file under cards/, parsed and validated; a bad file is an error."""
    cards = []
    for path in sorted((Path(root) / "cards").glob("C*.md")):
        card = schema.parse(path.read_text())
        errors = schema.validate(card)
        if errors:
            raise ValueError(f"{path.name}: " + "; ".join(errors))
        card["_file"] = path.name
        cards.append(card)
    return cards


def admitted(cards: list[dict]) -> list[dict]:
    """Admitted cards in serving order: earliest block first, then most support, then id."""
    return sorted((c for c in cards if c.get("admitted") is not None),
                  key=lambda c: (int(c["admitted"]), -len(schema.support_list(c.get("support"))), c["id"]))


def layout(root: Path) -> dict:
    root = Path(root)
    preamble = (root / "PREAMBLE.md").read_text().rstrip() + "\n"
    parts, size, rendered, overflow = [preamble], len(preamble.encode()), [], []
    for card in admitted(load(root)):
        text = "\n" + schema.render(card)
        if overflow or size + len(text.encode()) > CARDS_MAX:
            overflow.append(card["id"])
            continue
        parts.append(text)
        size += len(text.encode())
        rendered.append(card["id"])
    if not rendered:
        parts.append("\n" + EMPTY)
        size += len(EMPTY.encode()) + 1
    return {"text": "".join(parts), "bytes": size, "rendered": rendered, "overflow": overflow}


def compile(root: Path) -> str:
    return layout(root)["text"]
