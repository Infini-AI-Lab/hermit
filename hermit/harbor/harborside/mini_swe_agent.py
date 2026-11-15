"""HermitMiniSweAgent: mini-swe-agent in a Harbor trial, its session launched from Hermit's plan (harborside.hermit).

Imported by Harbor as `harborside.mini_swe_agent:HermitMiniSweAgent`. Harbor's mini-swe-agent brings the install: uv,
Python 3.12 and `uv tool install mini-swe-agent==<version> --with litellm ...` as the agent user (Hermit's runner gives
its tested build when the config names none), after the apt repair of an image whose release left the mirrors.

Hermit's mini-swe-agent session is no command of mini-swe-agent's own but Hermit's runner of one task
(hermit.agent.base.mini_swe_agent_run), whose stdout is Hermit's event stream. The plan therefore brings a bundle of
Hermit's modules (`bundle`, uploaded under its container_dir) and a command whose interpreter is @@HERMIT_PYTHON@@
(Hermit's `bin` setting; a host interpreter's path there is taken for it too): this class finds mini-swe-agent's own
Python, the uv tool's (`uv tool dir`, else the shebang of the tool's `mini-swe-agent`), puts the bundle first on
PYTHONPATH and checks once that the runner imports there. Then the session runs Hermit's runner as every other CLI's
plan runs (the prompt on stdin, the stream agent-mini-swe-agent.jsonl, the tools through HERMIT_TOOLS_URL).

If the runner cannot run there (the plan has no bundle, mini-swe-agent's Python is not found, or the runner does not
import in it), the trial raises HermitLaunchError saying why: another agent (mini-swe-agent's own command) in its
place would be a result of something else.
"""
from __future__ import annotations

from pathlib import PurePosixPath
import re
import shlex

from harbor.agents.installed.mini_swe_agent import MiniSweAgent, MiniSweAgentOptions
from harbor.environments.base import BaseEnvironment

from harborside.hermit import HermitInstalled, HermitLaunchError, HermitOptions, marked

PYTHON_MARK = "@@HERMIT_PYTHON@@"
RUNNER = "hermit.agent.base.mini_swe_agent_run"
INTERPRETER = re.compile(r"python[0-9.]*")
PYTHON_LINE = "hermit-python:"                    # FIND_PYTHON's answer, on a marked line (harborside.hermit.marked)
UV_ENV = ('if [ -f "$HOME/.local/bin/env" ]; then . "$HOME/.local/bin/env"; fi; '
          'export PATH="$HOME/.local/bin:$PATH"; ')
FIND_PYTHON = (UV_ENV + 'd=$(uv tool dir 2>/dev/null || true); '
               'if [ -n "$d" ] && [ -x "$d/mini-swe-agent/bin/python" ]; then '
               f'echo "{PYTHON_LINE}$d/mini-swe-agent/bin/python"; '
               'exit 0; fi; b=$(command -v mini-swe-agent 2>/dev/null || true); '
               'if [ -n "$b" ]; then l=$(head -n 1 "$b"); case "$l" in "#!/"*) set -- ${l#??}; '
               f'if [ -x "$1" ]; then echo "{PYTHON_LINE}$1"; exit 0; fi;; esac; fi; exit 1')


class HermitMiniSweAgentOptions(HermitOptions, MiniSweAgentOptions):
    """Harbor's mini-swe-agent kwargs (accepted, unused: the plan holds the session) and Hermit's. `extra="forbid"`."""


class HermitMiniSweAgent(HermitInstalled, MiniSweAgent):
    CLI = "mini-swe-agent"
    PROGRAM = "mini-swe-agent"
    options_model = HermitMiniSweAgentOptions
    options: HermitMiniSweAgentOptions

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._python: str | None = None

    async def _find_python(self, environment: BaseEnvironment) -> str | None:
        """mini-swe-agent's own Python (the uv tool's), found once per container."""
        if self._python is None:
            found = await environment.exec(command=FIND_PYTHON)
            path = marked(found.stdout, PYTHON_LINE) if found.return_code == 0 else None
            self._python = path if path and path.startswith("/") else None
        return self._python

    async def tool_python(self, environment: BaseEnvironment) -> str:
        return await self._find_python(environment) or "/usr/bin/env python3"

    def _pythonpath(self, plan: dict, workdir: str) -> str:
        """The bundle first on PYTHONPATH, then the rest of the plan's own PYTHONPATH (which may name it too)."""
        bundle = str(plan["bundle"]["container_dir"]).rstrip("/")
        own = self._fill((plan.get("env") or {}).get("PYTHONPATH") or "", workdir).split(":")
        return ":".join([bundle, *(p for p in own if p and p.rstrip("/") != bundle)])

    async def _bundle_problem(self, environment: BaseEnvironment, plan: dict, workdir: str) -> str | None:
        """Why Hermit's runner cannot run here, or None: no bundle, no mini-swe-agent Python, or no import."""
        if not plan.get("bundle"):
            return "the plan has no bundle of Hermit's modules"
        python = await self._find_python(environment)
        if python is None:
            return "mini-swe-agent's Python was not found (uv tool dir, the tool's shebang)"
        probe = await environment.exec(command=f"cd {_q(workdir)} && {_q(python)} -P -c {_q('import ' + RUNNER)} 2>&1",
                                       env={"PYTHONPATH": self._pythonpath(plan, workdir)})
        if probe.return_code != 0:
            return f"{RUNNER} does not import in {python}: {(probe.stdout or probe.stderr or '').strip()[-400:]}"
        return None

    async def session(self, environment: BaseEnvironment, plan: dict, workdir: str) -> dict:
        own = await super().session(environment, plan, workdir)
        problem = await self._bundle_problem(environment, plan, workdir)
        if problem is not None:
            raise HermitLaunchError(f"hermit: Hermit's mini-swe-agent runner cannot run in this container: {problem}")
        python = self._python
        argv = [python if a == PYTHON_MARK or (i == 0 and _interpreter(a)) else a.replace(PYTHON_MARK, python)
                for i, a in enumerate(own["argv"])]
        return {**own, "argv": argv, "env": {"PYTHONPATH": self._pythonpath(plan, workdir)}}


def _q(value: str) -> str:
    return shlex.quote(str(value))


def _interpreter(arg: str) -> bool:
    """A Python interpreter's path or name (a host's sys.executable a plan left in the command)."""
    return bool(INTERPRETER.fullmatch(PurePosixPath(arg).name))
