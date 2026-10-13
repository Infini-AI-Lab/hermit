#!/usr/bin/env python3
"""attached-delegation demo: grower loss-report triage, log attached.

Delegation here is ONE call to a fresh, focused instance with no tools
and no memory of the conversation. `fake_delegate` is a deterministic
offline stub — what this demo exercises is the PROTOCOL around the call:
request assembly (spec first, task, then raw material verbatim) and the
SPEC clauses that make the delegate trust attachments over retellings
and report contradictions.

Usage: python3 run.py
"""

import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "sample_data")

# The requester's retelling misstates both the count and the location.
TASK = ("Grower HALVERSEN reports 9 trays of CV-1188 were lost in "
        "coldframe and wants the loss set against their propagation quota. "
        "Triage the report.")


def build_request(spec_path, task, attach_paths):
    """Form: spec (first line `trigger: ...`), then the task, then every
    attachment verbatim — raw material, never a summary of it."""
    request = open(spec_path).read().strip() + "\n\n" + task
    for p in attach_paths:
        body = open(p, errors="replace").read()
        request += ("\n\n===== RAW MATERIAL: %s (verbatim; trust it over "
                    "any retelling) =====\n%s" % (os.path.basename(p), body))
    return request


def fake_delegate(prompt):
    """Deterministic stand-in for the fresh instance."""
    claim = re.search(r"reports (\d+) trays of (\S+) were lost in (\w+)",
                      prompt)
    if "===== RAW MATERIAL" not in prompt:
        # No raw material: the stub, like a real delegate, can only take
        # the retelling at face value — the confident wrong answer.
        return ("TRIAGE: accept %s trays of %s against the propagation quota "
                "(per task statement)" % (claim.group(1), claim.group(2)))
    logged = re.search(r"trays_lost:\s*(\d+)", prompt)
    noted = re.search(r"loss_noted_at:\s*([^\n]+)", prompt)
    lines, contradictions = [], []
    if logged and claim and logged.group(1) != claim.group(1):
        contradictions.append(
            "CONTRADICTION: task statement says %s trays lost; the "
            "attached log records %s" % (claim.group(1), logged.group(1)))
    if noted and claim and \
            not noted.group(1).strip().startswith(claim.group(3)):
        contradictions.append(
            "CONTRADICTION: task statement says the loss occurred in %s; the "
            "attached log notes it at the %s"
            % (claim.group(3), noted.group(1).strip()))
    if contradictions:
        lines.append("TRIAGE: hold — task statement conflicts with the "
                     "attached log; resolve before any quota decision")
        lines.extend(contradictions)
    else:
        lines.append("TRIAGE: accept per the attached log")
    return "\n".join(lines)


def triage(with_attachment=True):
    attach = [os.path.join(DATA, "germination_log.txt")] if with_attachment \
        else []
    request = build_request(os.path.join(DATA, "triage.spec"), TASK, attach)
    return request, fake_delegate(request)


def main():
    request, reply = triage(with_attachment=True)
    print("--- with the log attached ---")
    print(reply)
    _, bare = triage(with_attachment=False)
    print("--- same task, retelling only ---")
    print(bare)
    return request, reply, bare


if __name__ == "__main__":
    main()
