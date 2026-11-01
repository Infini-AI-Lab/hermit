#!/usr/bin/env python3
"""raw-input-classifier demo: classifying a pasted germination-log excerpt.

The actor pastes raw log rows verbatim and points at the rules
parameters; classification happens here. Rows that do not match the
strict grammar (retyped, paraphrased, truncated) are refused with a
CATCH line instead of being guessed at. An unparseable rules file
refuses the entire run.

Usage: python3 run.py sample_data/germination_log.txt sample_data/rules.json
"""

import json
import re
import sys

ROW = re.compile(
    r"^GRM\|(?P<id>G-\d+)\|(?P<date>\d{4}-\d{2}-\d{2})\|(?P<seedbank>SB-[A-Z]+)"
    r"\|(?P<crop>[a-z ]+)\|TRAYS=(?P<qty>\d+)\|COND=(?P<cond>[A-Z]+)"
    r"\|BENCH=(?P<bench>Z\d+)(?:\|PEST=(?P<pest>P-\d+))?$")


def load_rules(path):
    """The rules input is parsed strictly too: not valid JSON with the
    required keys means classification refuses to run at all."""
    try:
        rules = json.load(open(path))
        for key in ("bench_zones", "zone_caps", "conditions"):
            if key not in rules:
                return None
        return rules
    except (OSError, ValueError):
        return None


def classify_line(n, line, rules):
    m = ROW.match(line.strip())
    if not m:
        return (f"CATCH: line {n} is not a verbatim log row; "
                "paste the raw record unaltered")
    r = m.groupdict()
    zone = rules["bench_zones"].get(r["bench"])
    if zone is None:
        return f"VERDICT|{r['id']}|REJECT|unknown bench {r['bench']}"
    if r["cond"] not in rules["conditions"]:
        return f"VERDICT|{r['id']}|REJECT|unknown condition code {r['cond']}"
    cap = rules["zone_caps"][zone]
    if int(r["qty"]) > cap:
        return (f"VERDICT|{r['id']}|REJECT|trays {r['qty']} exceed "
                f"{zone} zone cap {cap}")
    if r["cond"] == "WILTED" and not r["pest"]:
        return (f"VERDICT|{r['id']}|HOLD|wilted trays need a pest-report "
                "tag before benching")
    return (f"VERDICT|{r['id']}|ACCEPT|trays {r['qty']} within {zone} "
            f"cap {cap}, condition {r['cond']}")


def main(log_path, rules_path):
    rules = load_rules(rules_path)
    if rules is None:
        print("CATCH: rules input unparseable; refusing to classify anything")
        return None
    out = [classify_line(n, line, rules)
           for n, line in enumerate(open(log_path), 1) if line.strip()]
    for v in out:
        print(v)
    return out


if __name__ == "__main__":
    args = sys.argv[1:] or ["sample_data/germination_log.txt",
                            "sample_data/rules.json"]
    sys.exit(0 if main(*args) is not None else 1)
