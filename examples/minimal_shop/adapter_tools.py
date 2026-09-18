"""Small deterministic adapter capabilities; no model or host dependencies."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CENT = Decimal("0.01")


def _memories() -> dict:
    return json.loads((ROOT / "memory" / "policies.json").read_text(encoding="utf-8"))


def list_memories() -> dict:
    return {
        "adapter": "minimal_shop_adapter",
        "memories": [{"id": key, "title": value["title"]} for key, value in _memories().items()],
    }


def read_memory(memory_id: str) -> dict:
    memories = _memories()
    if memory_id not in memories:
        raise ValueError(f"Unknown memory ID: {memory_id!r}; available: {', '.join(memories)}")
    return {
        "adapter": "minimal_shop_adapter",
        "id": memory_id,
        "source": f"memory/policies.json#{memory_id}",
        "memory": memories[memory_id],
    }


def _decimal(value: str, field: str) -> Decimal:
    if not isinstance(value, str) or not value.strip() or len(value) > 40:
        raise ValueError(f"{field} must be a short decimal string")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{field} must be a decimal string") from exc
    if not result.is_finite():
        raise ValueError(f"{field} must be finite")
    return result


def calculate_refund(item_subtotal: str, fee_rate: str) -> dict:
    subtotal = _decimal(item_subtotal, "item_subtotal")
    rate = _decimal(fee_rate, "fee_rate")
    if not Decimal("0") <= subtotal <= Decimal("1000000"):
        raise ValueError("item_subtotal must be between 0 and 1000000")
    if not Decimal("0") <= rate <= Decimal("1"):
        raise ValueError("fee_rate must be between 0 and 1")
    if subtotal != subtotal.quantize(CENT):
        raise ValueError("item_subtotal must have at most two decimal places")
    fee = (subtotal * rate).quantize(CENT, rounding=ROUND_HALF_UP)
    refund = subtotal - fee
    return {
        "currency": "CNY",
        "item_subtotal": format(subtotal, ".2f"),
        "fee_rate": str(rate),
        "fee": format(fee, ".2f"),
        "refund": format(refund, ".2f"),
        "rounding": "ROUND_HALF_UP; fee rounded before subtraction",
        "note": "Calculation only; check eligibility and shipping policy separately.",
    }
