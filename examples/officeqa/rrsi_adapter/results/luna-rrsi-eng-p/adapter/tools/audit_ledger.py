#!/usr/bin/env python3
"""Bounded validation of an analyst's extracted operand ledger."""
import argparse
import math
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-count", type=int, required=True)
    args = parser.parse_args()
    rows, errors = [], []
    for lineno, raw in enumerate(sys.stdin, 1):
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) != 6:
            errors.append(f"line {lineno}: expected 6 tab-separated fields")
            continue
        period, value, source, row, column, unit = fields
        missing = [name for name, item in (("period", period), ("value", value),
                                            ("source", source), ("row", row),
                                            ("column", column), ("unit", unit)) if not item.strip()]
        if missing:
            errors.append(f"line {lineno}: missing {', '.join(missing)}")
        try:
            number = float(value.replace(",", ""))
            if not math.isfinite(number):
                raise ValueError
        except ValueError:
            errors.append(f"line {lineno}: value is not a finite number: {value!r}")
        rows.append((period, lineno))
    if len(rows) != args.expected_count:
        errors.append(f"record count {len(rows)} != expected {args.expected_count}")
    seen = {}
    for period, lineno in rows:
        if period in seen:
            errors.append(f"duplicate period {period!r} on lines {seen[period]} and {lineno}")
        else:
            seen[period] = lineno
    if errors:
        print("LEDGER INVALID")
        for error in errors:
            print(f"- {error}")
        return 1
    print(f"LEDGER OK: {len(rows)} unique, numeric, fully annotated operands")
    return 0


if __name__ == "__main__":
    sys.exit(main())
