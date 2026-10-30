"""Keep the machine's Codex session slots (SLOTS, see codex_login.slot) below the login's rate limit.

Every 2 minutes, over the last 10: the share of finished sessions whose log shows a 429 (the streams' Codex sessions
and the relays' calls, which keep their whole event stream) and the share of SkillOpt-harness rollouts that timed out
(an empty codex_raw.txt: SkillOpt keeps no output of a session past its 180 s). Too many of either: two slots fewer
(not below MIN). Both rare for ten minutes since the last change: one slot more (not above MAX). Every change is a line
in logs/slots.log and on stdout.

    python slots_ctl.py [--min 2] [--max 16]
"""
import argparse
import glob
import os
import time
from pathlib import Path

LUNA = Path(os.path.expanduser(os.environ.get("LUNA", "~/albz/officeqa-luna")))
SLOTS = LUNA / "SLOTS"
WINDOW, EVERY, SETTLE, DOWN_SETTLE = 600, 120, 600, 360


def recent(pattern: str, since: float) -> list[str]:
    out = []
    for f in glob.glob(pattern):
        try:
            if os.stat(f).st_mtime >= since:
                out.append(f)
        except OSError:
            pass
    return out


def measure() -> tuple[int, int, int, int]:
    since = time.time() - WINDOW
    logs = recent(str(LUNA / "cache/*/rollouts/*/log/agent-codex.jsonl"), since) + \
        recent(str(LUNA / "relay/*/events.jsonl"), since)
    n429 = 0
    for f in logs:
        try:
            n429 += "Too Many Requests" in open(f, errors="replace").read()
        except OSError:
            pass
    raws = recent(str(LUNA / "cache/luna-[gs]*/rollouts/*/predictions/*/codex_raw.txt"), since)
    timed_out = sum(1 for f in raws if os.path.getsize(f) == 0)
    return len(logs), n429, len(raws), timed_out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--min", type=int, default=2)
    parser.add_argument("--max", type=int, default=16)
    args = parser.parse_args()
    changed = time.time()
    while True:
        try:
            slots = int(SLOTS.read_text().strip())
        except (OSError, ValueError):
            slots = args.min
        sessions, s429, rollouts, timed_out = measure()
        f429 = s429 / sessions if sessions >= 5 else None
        ftime = timed_out / rollouts if rollouts >= 10 else None
        new = slots
        if ((ftime is not None and ftime > 0.06) or (f429 is not None and f429 > 0.30)) \
                and time.time() - changed >= DOWN_SETTLE:          # not twice for the same events in the window
            new = max(args.min, slots - 2)
        elif ((ftime is None or ftime <= 0.03) and (f429 is None or f429 <= 0.10)
              and time.time() - changed >= SETTLE):
            new = min(args.max, slots + 1)
        line = (f"{time.strftime('%Y-%m-%d %H:%M:%S')} slots {slots} -> {new}  (last 10 min: {s429}/{sessions} "
                f"sessions with 429, {timed_out}/{rollouts} rollouts timed out)")
        if new != slots:
            SLOTS.write_text(f"{new}\n")
            changed = time.time()
            print(line, flush=True)
        with open(LUNA / "logs" / "slots.log", "a") as log:
            log.write(line + "\n")
        time.sleep(EVERY)


if __name__ == "__main__":
    main()
