#!/usr/bin/env python3
"""Small local calculator for standard statistics after the analyst fixes semantics."""
import argparse
import math
import statistics


def values(text):
    return [float(x) for x in text.replace(",", " ").split()]


def main():
    p = argparse.ArgumentParser(description="Calculate a standard statistic from explicit values.")
    p.add_argument("operation", choices=("mean", "geomean", "sd", "percentile", "trimmed_mean"))
    p.add_argument("data", help="space- or comma-separated values, already transformed as required")
    p.add_argument("--sample", action="store_true", help="sample standard deviation (sd only)")
    p.add_argument("--q", type=float, help="percentile in [0, 1] (linear interpolation)")
    p.add_argument("--trim", type=float, help="fraction removed from each end (trimmed_mean only)")
    args = p.parse_args()
    xs = values(args.data)
    if not xs:
        p.error("at least one value is required")
    if args.operation == "mean":
        result = statistics.fmean(xs)
    elif args.operation == "geomean":
        if any(x <= 0 for x in xs):
            p.error("geomean requires positive values")
        result = math.exp(statistics.fmean(math.log(x) for x in xs))
    elif args.operation == "sd":
        if len(xs) < (2 if args.sample else 1):
            p.error("not enough values for requested standard deviation")
        result = statistics.stdev(xs) if args.sample else statistics.pstdev(xs)
    elif args.operation == "percentile":
        if args.q is None or not 0 <= args.q <= 1:
            p.error("--q must be between 0 and 1")
        ys = sorted(xs)
        position = (len(ys) - 1) * args.q
        lo = math.floor(position)
        hi = math.ceil(position)
        result = ys[lo] if lo == hi else ys[lo] + (position - lo) * (ys[hi] - ys[lo])
    else:
        if args.trim is None or not 0 <= args.trim < 0.5:
            p.error("--trim must be in [0, 0.5)")
        ys = sorted(xs)
        cut = int(len(ys) * args.trim)
        if 2 * cut >= len(ys):
            p.error("trim removes all values")
        result = statistics.fmean(ys[cut:len(ys) - cut or None])
    print(repr(result))


if __name__ == "__main__":
    main()
