"""A worker's calls of the Trainer's tools (`POST <endpoint>/call/<name>`): `llm`, `check_spec`, `run_agent`."""
from __future__ import annotations

import json
import urllib.request

TIMEOUT_S = None                     # no clock here: `run_agent` and `llm` have their own, and the Trainer's call clock
                                     # stops the worker


def call_tool(url: str, name: str, payload: dict) -> dict:
    """The tool's result, or its error raised."""
    request = urllib.request.Request(url + "/call/" + name, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        reply = json.loads(response.read())
    if reply.get("error"):
        raise RuntimeError(f"tool {name}: {reply['error']}")
    return reply["result"]
