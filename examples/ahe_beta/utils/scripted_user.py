"""A scripted user, for checking the channel without a user model (../smoke/): it inserts a message while the agent
works, asks a follow-up, interrupts it, asks for a summary, looks at the workspace and scores what of the channel
worked. Its instructions suit the smoke's task (a file the agent appends lines to), so a real model follows them.

    def simulate(task, agent, workspace, params) -> dict          (user.py's contract)
"""

import json
import time

INSERT = "While you are at it: after step 2, also run `echo inserted >> /app/a.txt` as its own command."
FOLLOWUP = ("Now run `sleep 20 && echo three >> /app/a.txt`, then `echo four >> /app/a.txt`, each as its own shell "
            "command, and reply when both are done.")
INTERRUPT = "Stop, I changed my mind: do not add four."
FINAL = "Reply with the contents of /app/a.txt, in one line."


def _until_reply(agent, seen: list, timeout: float, on_events=None):
    """The agent's next reply, watching its events meanwhile (on_events may act on them and return a reply)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        reply = agent.poll(min(5.0, max(0.0, deadline - time.monotonic())))
        events = agent.events()
        seen += events
        if reply:
            return reply
        if on_events is not None:
            acted = on_events(events)
            if acted:
                return acted
    raise TimeoutError(f"no reply within {timeout:.0f} s")


def simulate(task: dict, agent, workspace, params: dict) -> dict:
    timeout = float(params.get("turn_timeout_s", 600))
    seen, replies, inserted = [], [], []
    agent.write(task["instruction"].strip())                       # starts Codex with it as the prompt

    def insert_once(events):
        if not inserted and any(e.get("kind") == "tool" for e in seen):
            agent.write(INSERT)                                    # reaches the model after the running tool call
            inserted.append(True)
        return None

    replies.append(_until_reply(agent, seen, timeout, insert_once))
    agent.write(FOLLOWUP)

    def interrupt_at_three(events):
        if any(e.get("kind") == "tool_start" and "three" in json.dumps(e.get("input"), default=str) for e in events):
            return agent.interrupt(INTERRUPT, timeout=timeout) or {"kind": "reply", "text": "", "interrupted": None}
        return None

    replies.append(_until_reply(agent, seen, timeout, interrupt_at_three))
    replies.append({"kind": "reply", "text": agent.send(FINAL, timeout=timeout)})
    seen += agent.events()
    content = workspace.run("cat /app/a.txt")["stdout"]
    lines = content.split()
    checks = {
        "insert_delivered": any(e.get("kind") == "delivered" and e.get("at") == "PostToolUse" for e in seen),
        "inserted_line": "inserted" in lines,
        "followup_done": "three" in lines,
        "interrupt_acknowledged": any(r.get("interrupted") for r in replies),
        "four_not_added": "four" not in lines,
        "final_reply": bool(replies[-1].get("text", "").strip()),
    }
    score = sum(checks.values()) / len(checks)
    failed = [name for name, ok in checks.items() if not ok]
    return {
        "score": score,
        "rating": round(1 + 3 * score),
        "outcome": "accept" if not failed else "accept_with_reservations",
        "reason": "every step of the channel worked" if not failed else f"not seen: {', '.join(failed)}",
        "issues": [{"title": f"channel check failed: {name}", "body": "seen in the smoke's scripted session"}
                   for name in failed],
        "record": {"checks": checks, "replies": replies, "workspace": content, "params": params,
                   "kinds_seen": sorted({str(e.get("kind")) for e in seen})},
        "files": {"session.json": json.dumps({"checks": checks}, indent=1) + "\n"},
    }
