#!/usr/bin/env python3
"""The user channel, container side: three Codex hooks that carry a simulated user's messages into a running session.

    python3 user_hook.py PreToolUse | PostToolUse | Stop        (registered in <workdir>/.codex/hooks.json)

Standard library only. The settings are in `user_hook.json` beside this file: `channel` (the root-owned bridge socket),
`wait_s` (how long a Stop waits for the user), and `adapter_stop` (the adapter's own Stop commands, run here before
the user hears anything). A legacy `mail` setting remains supported for offline hook tests.

The legacy mailbox:

    to-user/<time>-<pid>-<kind>.json     written here:        tool_start, tool, reply, delivered, timeout
    to-agent/<time>-<kind>.json          written by the host: message, interrupt, end
    taken/                               what this hook has delivered or consumed

What each event does:

    PreToolUse    reports the tool call that starts; while an `interrupt` is waiting, denies the call with the
                  user's words (every call, until the agent stops)
    PostToolUse   reports the finished tool call; hands waiting messages to the model as additional context
    Stop          runs the adapter's Stop hooks first (a block from one of them is passed on and the user hears
                  nothing yet); then reports the agent's reply and waits: a message continues the session with the
                  user's words, `end` lets it stop
"""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve()
CFG = json.loads(HERE.with_suffix(".json").read_text())
MAIL = Path(CFG.get("mail") or "/run/ahe-user-channel/legacy-mail")
TO_USER, TO_AGENT, TAKEN = MAIL / "to-user", MAIL / "to-agent", MAIL / "taken"
CHANNEL = Path(CFG["channel"]) if CFG.get("channel") else None
SHORT = 20000  # characters of a tool's input or output reported to the user


def report(kind: str, **fields) -> None:
    """One event for the user, written whole (a temporary name, then a rename)."""
    if CHANNEL is not None:
        response = _request({"op": "report", "event": {"kind": kind, "t": round(time.time(), 3), **fields}})
        if not response.get("ok"):
            raise RuntimeError(response.get("error") or "user channel report failed")
        return
    TO_USER.mkdir(parents=True, exist_ok=True)
    name = f"{time.time():017.6f}-{os.getpid()}-{kind}.json"
    tmp = TO_USER / ("." + name)
    tmp.write_text(json.dumps({"kind": kind, "t": round(time.time(), 3), **fields}))
    os.chmod(tmp, 0o666)
    os.replace(tmp, TO_USER / name)


def _request(payload: dict) -> dict:
    if CHANNEL is None:
        return {}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(10)
        connection.connect(str(CHANNEL))
        connection.sendall(json.dumps(payload, ensure_ascii=False).encode() + b"\n")
        data = bytearray()
        while len(data) < 256 * 1024:
            chunk = connection.recv(4096)
            if not chunk:
                break
            data.extend(chunk)
            if b"\n" in chunk:
                break
    try:
        response = json.loads(bytes(data).splitlines()[0])
    except (IndexError, ValueError):
        return {"ok": False, "error": "invalid channel response"}
    if not isinstance(response, dict) or not response.get("ok"):
        return {"ok": False, "error": str((response or {}).get("error") or "channel request failed")}
    return response


def waiting(kind: str) -> list[Path] | list[dict]:
    if CHANNEL is not None:
        response = _request({"op": "peek", "kind": kind})
        if not response.get("ok"):
            raise RuntimeError(response.get("error") or "user channel read failed")
        return list(response.get("messages") or [])
    return sorted(TO_AGENT.glob(f"*-{kind}.json")) if TO_AGENT.is_dir() else []


def take(paths: list[Path] | list[dict]) -> list[dict]:
    """Claim the files (a rename: two hooks running at once never deliver the same message twice)."""
    if CHANNEL is not None:
        names = [str(value.get("_file")) for value in paths if isinstance(value, dict) and value.get("_file")]
        if not names:
            return []
        kind = str(next((value.get("kind") for value in paths if isinstance(value, dict)), ""))
        response = _request({"op": "take", "kind": kind, "names": names})
        if not response.get("ok"):
            raise RuntimeError(response.get("error") or "user channel claim failed")
        return [dict(value) for value in response.get("messages") or []]
    TAKEN.mkdir(parents=True, exist_ok=True)
    out = []
    for path in paths:
        try:
            os.replace(path, TAKEN / path.name)
        except OSError:
            continue
        try:
            out.append(json.loads((TAKEN / path.name).read_text()))
        except ValueError:
            pass
    return out


def short(value) -> str:
    text = (
        value
        if isinstance(value, str)
        else json.dumps(value, ensure_ascii=False, default=str)
    )
    return (
        text
        if len(text) <= SHORT
        else text[:SHORT] + f" ... [{len(text) - SHORT} more characters]"
    )


def said(messages: list[dict], frame: str) -> str:
    return frame.format(text="\n\n".join(str(m.get("text") or "") for m in messages))


def adapter_stop(raw: str, payload: dict) -> dict | None:
    """The adapter's own Stop hooks, one after another; the first block, or None when all of them let the agent
    stop. Codex would run them at the same time as this hook, so the user would hear a reply the adapter is still
    sending back for more work."""
    passthrough = {}
    for hook in CFG.get("adapter_stop") or []:
        try:
            run = subprocess.run(
                hook["command"],
                shell=True,
                input=raw,
                capture_output=True,
                text=True,
                check=False,
                cwd=payload.get("cwd") or None,
                timeout=float(hook.get("timeout") or 60),
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if run.returncode == 2:  # the exit-code form of a block: stderr is the reason
            return {
                "decision": "block",
                "reason": run.stderr.strip() or "blocked by the adapter's Stop hook",
            }
        try:
            out = json.loads(run.stdout or "{}")
        except ValueError:
            continue
        if isinstance(out, dict) and (
            out.get("decision") == "block" or out.get("continue") is False
        ):
            return out
        if isinstance(out, dict) and out.get("systemMessage"):
            report("adapter_message", text=out["systemMessage"])
            passthrough["systemMessage"] = "\n".join(
                filter(
                    None, [passthrough.get("systemMessage"), str(out["systemMessage"])]
                )
            )
    return passthrough or None


def main() -> None:
    event = sys.argv[1]
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw or "{}")
    except ValueError:
        payload = {}
    tool = {"tool": payload.get("tool_name"), "input": payload.get("tool_input")}
    if event == "PreToolUse":
        interrupts = waiting("interrupt")
        if interrupts:  # left in place: every call is denied until the agent stops
            try:
                value = interrupts[0]
                text = (
                    (value.get("text") if isinstance(value, dict) else json.loads(value.read_text()).get("text"))
                    or ""
                )
            except (OSError, ValueError, AttributeError):
                text = ""
            report("denied", **tool)
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason": CFG["interrupt_frame"].format(
                                text=text
                            ),
                        }
                    }
                )
            )
            return
        report("tool_start", **tool)
        return
    if event == "PostToolUse":
        report("tool", **tool, output=payload.get("tool_response"))
        messages = take(waiting("message"))
        if messages:
            report(
                "delivered",
                at="PostToolUse",
                ids=[m.get("id") for m in messages],
                **tool,
            )
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "PostToolUse",
                            "additionalContext": said(messages, CFG["insert_frame"]),
                        }
                    }
                )
            )
        return
    if event != "Stop":
        return
    passthrough = {}
    interrupted = bool(take(waiting("interrupt")))
    ends = waiting("end")
    if not interrupted and not ends:
        block = adapter_stop(raw, payload)
        if block and (
            block.get("decision") == "block" or block.get("continue") is False
        ):
            report(
                "adapter_stop" if block.get("continue") is False else "adapter_block",
                reason=short(block.get("stopReason") or block.get("reason") or ""),
            )
            if block.get("continue") is False:
                report(
                    "reply",
                    text=payload.get("last_assistant_message") or "",
                    interrupted=False,
                    turn=payload.get("turn_id"),
                    terminal=True,
                )
            print(json.dumps(block))
            return
        passthrough = block or {}
    report(
        "reply",
        text=payload.get("last_assistant_message") or "",
        interrupted=interrupted,
        turn=payload.get("turn_id"),
    )
    deadline = time.monotonic() + float(CFG.get("wait_s") or 3600)
    while time.monotonic() < deadline:
        if waiting("end"):
            take(waiting("end"))
            if passthrough:
                print(json.dumps(passthrough))
            return
        interrupted_messages = take(waiting("interrupt"))
        if interrupted_messages:
            report(
                "interrupt_ack",
                ids=[m.get("id") for m in interrupted_messages],
                at="Stop",
            )
        messages = take(waiting("message"))
        if messages:
            report("delivered", at="Stop", ids=[m.get("id") for m in messages])
            print(
                json.dumps(
                    {
                        **passthrough,
                        "decision": "block",
                        "reason": said(messages, CFG["reply_frame"]),
                    }
                )
            )
            return
        if waiting("end"):
            return
        time.sleep(0.2)
    report("timeout", waited_s=float(CFG.get("wait_s") or 3600))


if __name__ == "__main__":
    main()
