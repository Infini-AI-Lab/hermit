"""A scripted user, for checking the channel without a model: it inserts a message while the agent works, asks a
follow-up, interrupts, asks once more, looks at the workspace and rates the session."""

import json


def simulate(task: dict, agent, workspace, params: dict) -> dict:
    replies, seen, inserted = [], [], False
    agent.write("FIRST: " + task["instruction"].strip())  # starts Codex
    while True:  # the agent is working: watch, and insert once
        reply = agent.poll(1.0)
        seen += agent.events()
        if reply:
            replies.append(reply)
            break
        if not inserted and any(e["kind"] == "tool" for e in seen):
            agent.write("USER-INSERT: use method B instead")
            inserted = True
    agent.write("USER-FOLLOWUP: now do part two")
    while True:  # interrupt when it starts the wrong thing
        reply = agent.poll(1.0)
        events = agent.events()
        seen += events
        if reply:
            replies.append(reply)
            break
        if any(
            e["kind"] == "tool_start" and "three" in json.dumps(e["input"])
            for e in events
        ):
            replies.append(
                agent.interrupt("USER-INTERRUPT: wrong direction", timeout=120)
            )
            break
    replies.append(
        {"kind": "reply", "text": agent.send("USER-FINAL: summarize", timeout=120)}
    )
    seen += agent.events()
    check = workspace.run("cat /app/a.txt")
    return {
        "score": 0.75,
        "rating": 3,
        "outcome": "accept_with_reservations",
        "reason": "did the work, but needed an interrupt",
        "issues": [
            {
                "title": "ignores the user's preferred method",
                "body": "had to interrupt",
                "labels": ["bug"],
            }
        ],
        "record": {
            "replies": replies,
            "workspace": check["stdout"],
            "params": params,
            "kinds_seen": sorted({e["kind"] for e in seen}),
        },
        "files": {
            "session.json": '{"note": "the simulator\'s own session record goes here"}\n'
        },
    }
