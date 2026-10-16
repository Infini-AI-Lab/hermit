"""CL-Bench poker judge: the hand's result in big blinds, plus the hands the task settled without the host on the
way (they count toward the venue's bb/hand exactly as natively; report the venue metric from outcomes.jsonl)."""


def score(task: dict, output) -> dict:
    extra = output.extra or {}
    own, settled = float(extra.get("reward_bb", 0.0)), float(extra.get("settled_reward_bb", 0.0))
    return {"score": own + settled, "feedback": f"net {output.output:+d} chips",
            "hand_bb": own, "settled_bb": settled, "settled_hands": extra.get("settled_without_host", [])}
