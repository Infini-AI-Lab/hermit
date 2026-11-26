"""One comparison of an answer with its reference by Math-Verify, in a process of its own (grade.equal starts it with a
time limit and kills it past that): {"given", "reference"} as JSON on stdin, {"equal": true|false|null} on stdout (null: past its memory bound). Both are
read as LaTeX, and only as LaTeX: Math-Verify's other extractor reads a plain expression with sympy's parse_expr, which
evaluates Python, and the answer is the model's text. Math-Verify's own timeouts apply here, in this process's main
thread; an answer whose arithmetic never returns (a tower of powers) is what grade.equal's limit is for."""
from __future__ import annotations

import json
import logging
import sys


def compare(given: str, reference: str) -> bool:
    from math_verify import LatexExtractionConfig, parse, verify
    logging.getLogger("math_verify").setLevel(logging.ERROR)
    latex = [LatexExtractionConfig()]
    gold = parse(f"${reference}$", extraction_config=latex)
    return bool(gold) and bool(verify(gold, parse(f"${given}$", extraction_config=latex)))


if __name__ == "__main__":
    try:                                # a bound on memory too, on Linux (macOS does not apply it): a power tower's digits would
                                        # fill it, and several judges compare at once
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (2 << 30, 2 << 30))
    except (ImportError, ValueError, OSError):
        pass
    pair = json.loads(sys.stdin.read())
    try:
        result = compare(pair["given"], pair["reference"])
    except MemoryError:                 # past the bound: not comparable, as past the time limit
        result = None
    print(json.dumps({"equal": result}))
