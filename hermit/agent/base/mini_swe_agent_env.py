"""HermitEnvironment: mini-swe-agent's local environment with the call's tools as one bash command.

mini-swe-agent's model runs bash commands and nothing else, so a tool is a command of its own:

    hermit-tool <name> '<JSON arguments>'      # the result is printed; an error exits 1
    hermit-tool                                # the tools, one per line

The environment answers it in mini-swe-agent's process and runs every other command as LocalEnvironment does. Here
the tools are the call's, served by the Base Agent's tool server (its JSON entrance, `tools_url`);
hermit.hosts.mini_swe_agent_env serves a deployed workspace's adapter tools the same way through a Session.

With `inherit_env: false` (the Base Agent) a command sees `env`, the basic variables in BASIC_ENV and `extra_env`
only, never the process's environment, where the model's key is. `extra_env` is not part of the config, which
mini-swe-agent writes into the trajectory. This module imports mini-swe-agent: import it in mini-swe-agent's process.
"""
from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
from typing import Any
import urllib.error
import urllib.request

from minisweagent.environments.local import LocalEnvironment, LocalEnvironmentConfig

TOOL_COMMAND = "hermit-tool"
TOOLS_ENV = "HERMIT_TOOLS_URL"           # the tool server's JSON entrance, as the Base Agent passes it
BASIC_ENV = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR",
             "TZ")
CALL_TIMEOUT_S = 600
RUNNING: set[int] = set()                # the process groups of the commands running now (stop_commands)


class HermitEnvironmentConfig(LocalEnvironmentConfig):
    tools_url: str = ""
    """The tool server's JSON entrance (GET <url>/tools, POST <url>/call/<name>); empty: the HERMIT_TOOLS_URL
    variable, else no tools."""
    inherit_env: bool = True
    """False: commands get `env`, BASIC_ENV and `extra_env` only, not this process's environment."""


class HermitEnvironment(LocalEnvironment):
    def __init__(self, *, extra_env: dict[str, str] | None = None, config_class: type = HermitEnvironmentConfig,
                 **kwargs):
        super().__init__(config_class=config_class, **kwargs)
        self.extra_env = dict(extra_env or {})

    def execute(self, action: dict, cwd: str = "", *, timeout: int | None = None) -> dict[str, Any]:
        command = action.get("command", "")
        try:
            words = shlex.split(command)
        except ValueError:
            words = []
        if words and words[0] == TOOL_COMMAND:
            return self._tool(words[1:])
        if self.config.inherit_env:
            return super().execute(action, cwd, timeout=timeout)
        env = {k: os.environ[k] for k in BASIC_ENV if k in os.environ} | self.config.env | self.extra_env
        try:
            result = _run(command, cwd or self.config.cwd or os.getcwd(), env, timeout or self.config.timeout)
            output = {"output": result.stdout, "returncode": result.returncode, "exception_info": ""}
        except Exception as e:                  # as LocalEnvironment.execute reports a command that failed to run
            raw = getattr(e, "output", None)
            raw = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else (raw or "")
            output = {"output": raw, "returncode": -1,
                      "exception_info": f"An error occurred while executing the command: {e}",
                      "extra": {"exception_type": type(e).__name__, "exception": str(e)}}
        self._check_finished(output)
        return output

    # ---------- the tools: the tool server's here, a Session's in a deployed workspace ----------
    def tool_list(self) -> list[dict]:
        """The tools: name, description and input schema of each."""
        url = self._url()
        return _request(f"{url}/tools") if url else []

    def call_tool(self, name: str, arguments: dict) -> str:
        """One call's result as the text the model reads; raises on an error."""
        url = self._url()
        if not url:
            raise ValueError(f"Unknown tool: {name}")
        answer = _request(f"{url}/call/{name}", arguments)
        if "error" in answer:
            raise RuntimeError(answer["error"])
        return text(answer.get("result"))

    def _url(self) -> str:
        return (self.config.tools_url or os.environ.get(TOOLS_ENV, "")).rstrip("/")

    def _tool(self, words: list[str]) -> dict[str, Any]:
        """A `hermit-tool` command's output: the tool list, the tool's result, or the error (exit 1)."""
        try:
            tools = self.tool_list()
        except Exception as exc:
            return _out(f"Error: {type(exc).__name__}: {exc}", 1)
        if not tools:
            return _out("Error: no tools are available here", 1)
        if not words:
            return _out("\n".join(f"{t['name']}: {t['description']}" for t in tools), 0)
        if len(words) > 2:
            return _out(f"Error: run {TOOL_COMMAND} as a command of its own: {TOOL_COMMAND} <name> '<JSON arguments>', "
                        "with the arguments as one JSON object in single quotes", 1)
        name = words[0]
        try:
            if name not in {t["name"] for t in tools}:
                raise ValueError(f"Unknown tool: {name}")
            arguments = json.loads(words[1]) if len(words) > 1 else {}
            if not isinstance(arguments, dict):
                raise ValueError("tool arguments must be a JSON object")
            return _out(self.call_tool(name, arguments), 0)
        except Exception as exc:
            return _out(f"Error: {type(exc).__name__}: {exc}", 1)


def _run(command: str, cwd: str, env: dict[str, str], timeout: int) -> subprocess.CompletedProcess[str]:
    """mini-swe-agent's own _run (a command in a process group of its own, the group killed at its timeout), with
    the group recorded while it runs, so that stop_commands reaches it."""
    process = subprocess.Popen(command, shell=True, text=True, cwd=cwd, env=env, encoding="utf-8", errors="replace",
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    RUNNING.add(process.pid)
    try:
        stdout, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        stdout, _ = process.communicate()
        raise subprocess.TimeoutExpired(command, timeout, output=stdout)
    finally:
        RUNNING.discard(process.pid)
    return subprocess.CompletedProcess(command, process.returncode, stdout=stdout)


def stop_commands() -> None:
    """Kill the commands running now: each runs in a session of its own, which a kill of this process's group does
    not reach (on macOS nothing else finds them: the Base Agent's stop walks /proc)."""
    for pid in list(RUNNING):
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def text(result: Any) -> str:
    """A tool result as the text a model reads: a result's own `text` field, else its JSON (as Session.text)."""
    if isinstance(result, dict) and isinstance(result.get("text"), str):
        return result["text"]
    return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)


def _request(url: str, body: dict | None = None) -> Any:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method="GET" if body is None else "POST",
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=CALL_TIMEOUT_S) as response:
            return json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:              # an unknown tool is a 404 with a JSON error
        return json.loads(error.read() or b"{}")


def _out(text: str, returncode: int) -> dict[str, Any]:
    return {"output": text if text.endswith("\n") or not text else text + "\n", "returncode": returncode,
            "exception_info": ""}


__all__ = ["BASIC_ENV", "HermitEnvironment", "HermitEnvironmentConfig", "TOOL_COMMAND", "TOOLS_ENV", "stop_commands"]
