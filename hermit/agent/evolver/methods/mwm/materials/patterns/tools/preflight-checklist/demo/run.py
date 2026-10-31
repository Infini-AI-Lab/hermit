#!/usr/bin/env python3
"""preflight-checklist demo: gating a transplant before it happens.

Takes an action type plus a facts JSON, checks the per-action
prerequisite list, and prints exactly one line: GO with the satisfied
checklist, or CATCH with what is missing. Unknown action types are
refused; unparseable facts are treated as NO-GO, never as GO.

Usage: python3 run.py sample_data/checklists.json sample_data/facts_go.json
"""

import json
import sys


def load_json(path):
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return None


def preflight(checklists, action, facts):
    checklist = checklists.get(action)
    if checklist is None:
        return f"CATCH: unknown action type '{action}'; no checklist on file, refusing"
    missing = [item for item in checklist if not facts.get(item)]
    if missing:
        return f"CATCH: {action} blocked - missing: {', '.join(missing)}"
    return f"GO|{action}|satisfied={','.join(checklist)}"


def main(checklists_path, facts_path):
    checklists = load_json(checklists_path)
    if checklists is None:
        line = "CATCH: checklist input unparseable; treating as NO-GO"
    else:
        req = load_json(facts_path)
        if req is None or "action" not in req or "facts" not in req:
            line = "CATCH: facts input unparseable; treating as NO-GO"
        else:
            line = preflight(checklists, req["action"], req["facts"])
    print(line)
    return line


if __name__ == "__main__":
    args = sys.argv[1:] or ["sample_data/checklists.json",
                            "sample_data/facts_go.json"]
    sys.exit(0 if main(*args).startswith("GO|") else 1)
