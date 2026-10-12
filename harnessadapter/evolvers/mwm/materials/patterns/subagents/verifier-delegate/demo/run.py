#!/usr/bin/env python3
"""verifier-delegate demo: checking a bench assignment.

Before an irreversible step (committing a seedling tray to a bench), a delegate
— one call to a fresh instance, no tools, no memory of the conversation
— re-derives the decision from attached raw materials only and must
answer in one machine-parseable VERDICT line. `fake_delegate` is a
deterministic offline stub — the demo exercises the PROTOCOL: request
assembly from raw material, the verdict grammar, the parser's
cannot-tell fallback, and the two-vote CATCH rule.

Usage: python3 run.py
"""

import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "sample_data")

GRAMMAR = ("Reply with exactly ONE line:\n"
           "VERDICT: agree\n"
           "VERDICT: differ <derived-value>\n"
           "VERDICT: cannot-tell <why>\n")


def build_request(proposal, attach_paths):
    """Form: raw materials only — no proposer reasoning rides along."""
    request = ("A bench assignment is about to be committed. Using ONLY the "
               "RAW MATERIAL below, independently derive the correct bench "
               "and compare it with the proposal.\n" + GRAMMAR
               + "\nPROPOSED: " + proposal)
    for p in attach_paths:
        body = open(p, errors="replace").read()
        request += ("\n\n===== RAW MATERIAL: %s (verbatim) =====\n%s"
                    % (os.path.basename(p), body))
    return request


def fake_delegate(prompt):
    """Deterministic stand-in for the fresh instance."""
    cls = re.search(r"climate_class:\s*(\w+)", prompt)
    if not cls:
        # A real model with the deciding field missing may ramble instead
        # of following the grammar — the caller must survive that.
        return ("Hmm, the record does not name a climate class, so it is "
                "hard to say; perhaps the proposal is fine, or perhaps "
                "the tray should be inspected first.")
    rules = dict(re.findall(r"(\w+)\s+->\s+zone (\w+)", prompt))
    benches = re.search(r"open benches:\s*([^\n]+)", prompt).group(1)
    zone = rules[cls.group(1)]
    slot = next(s.strip() for s in benches.split(",")
                if s.strip().startswith(zone + "-"))
    proposed = re.search(r"PROPOSED:\s*(\S+)", prompt).group(1)
    if slot == proposed:
        return "VERDICT: agree"
    return "VERDICT: differ %s" % slot


def parse_verdict(reply):
    """Anything unparseable IS cannot-tell — the fallback is defined."""
    m = re.match(r"VERDICT:\s*(agree|differ|cannot-tell)\s*(.*)",
                 reply.strip().splitlines()[0] if reply.strip() else "")
    if not m:
        return ("cannot-tell", "unparseable reply")
    return (m.group(1), m.group(2).strip())


def verify(proposal, attach_paths, delegate=fake_delegate, samples=2):
    """Two independent agreeing verdicts outrank the one proposer."""
    verdicts = [parse_verdict(delegate(build_request(proposal, attach_paths)))
                for _ in range(samples)]
    kinds = {k for k, _ in verdicts}
    values = {v for k, v in verdicts if k == "differ"}
    if kinds == {"differ"} and len(values) == 1:
        derived = values.pop()
        print("CATCH: proposal %s overruled by %d independent verdicts: %s"
              % (proposal, samples, derived))
        return ("CATCH", derived, verdicts)
    if kinds == {"agree"}:
        return ("confirmed", proposal, verdicts)
    # cannot-tell (or a split) never confirms: the cautious branch.
    return ("unverified", proposal, verdicts)


def main():
    rules = os.path.join(DATA, "zone_rules.txt")
    cases = [("A-6", "record_temperate.txt"),  # sound proposal
             ("A-6", "record_tropical.txt"),   # mis-zoned -> CATCH
             ("A-6", "record_garbled.txt")]    # missing field -> fallback
    results = []
    for proposal, record in cases:
        out = verify(proposal, [os.path.join(DATA, record), rules])
        print("%s + %s -> %s" % (record, proposal, out[0]))
        results.append(out)
    return results


if __name__ == "__main__":
    main()
