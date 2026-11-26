#!/usr/bin/env python3
"""G-Pass@k and mG-Pass@k, LiveMathBench's own metrics (Liu et al., 2024, arXiv:2412.13147), from the trials of a
`hermit eval` (its `results.jsonl`: each test question's attempts, `eval.repeats` of them, scored 1 or 0).

    python examples/livemathbench/utils/gpassk.py <eval folder> [--k 4]

For a question with n attempts of which c are right, G-Pass@k_τ is the chance that at least ⌈τ·k⌉ of k attempts drawn
from the n without replacement are right, Σ_{j=⌈τk⌉}^{k} C(c, j) C(n−c, k−j) / C(n, k) (τ = 0 is pass@k), averaged over
the questions; mG-Pass@k is (2/k) Σ_{i=⌈k/2⌉+1}^{k} G-Pass@k_{i/k}. The paper draws n = 48 (k up to 16); the configs
here run 4 (`eval.repeats`), which gives k up to 4: `eval.repeats=48` for the paper's. A failed attempt (a judgement
that failed, a run that is not the agent's result) counts as wrong, as `hermit eval`'s mean_score counts it, and is
counted apart.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
from math import ceil, comb
from pathlib import Path


def g_pass(n: int, c: int, k: int, tau: float) -> float:
    if tau <= 0:
        return 1.0 - comb(n - c, k) / comb(n, k)
    return sum(comb(c, j) * comb(n - c, k - j) for j in range(ceil(tau * k), k + 1)) / comb(n, k)


def metrics(counts: list[tuple[int, int]], k: int) -> dict:
    """{G-Pass@k_τ for τ in 0, 0.25, 0.5, 0.75, 1, and mG-Pass@k} over questions given as (n, c)."""
    if not counts or min(n for n, _ in counts) < k:
        raise ValueError(f"every question needs at least k = {k} attempts")
    mean = lambda tau: sum(g_pass(n, c, k, tau) for n, c in counts) / len(counts)
    out = {f"G-Pass@{k}_{tau:g}": mean(tau) for tau in (0, 0.25, 0.5, 0.75, 1.0)}
    out[f"mG-Pass@{k}"] = 2 / k * sum(mean(i / k) for i in range(ceil(k / 2) + 1, k + 1))
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("eval_dir", type=Path)
    parser.add_argument("--k", type=int, default=4)
    args = parser.parse_args()
    attempts, failed = defaultdict(list), 0
    for line in (args.eval_dir / "results.jsonl").read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        ok = row.get("status") == "ok"
        failed += not ok
        attempts[str(row["id"])].append(ok and float((row.get("judge") or {}).get("score") or 0) >= 1.0)
    counts = [(len(a), sum(a)) for a in attempts.values()]
    print(json.dumps({"questions": len(counts), "attempts": sum(n for n, _ in counts), "failed_attempts": failed,
                      "accuracy": sum(c for _, c in counts) / max(1, sum(n for n, _ in counts)),
                      **metrics(counts, args.k)}, indent=1))


if __name__ == "__main__":
    main()
