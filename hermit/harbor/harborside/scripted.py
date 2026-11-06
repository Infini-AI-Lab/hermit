"""A shell script as Harbor's agent: what a task set's packaging is checked with, without a model.

    agents:
      - import_path: harborside.scripted:ScriptedAgent
        kwargs: {script: "echo 42 > /app/answer.txt"}

The script runs in the task's environment as its default user, with the instruction in the file
`$HERMIT_INSTRUCTION_FILE` (`/logs/agent/instruction.md`); its output is kept in the agent's logs as `script.txt`.
A non-zero exit is the agent's failure (NonZeroAgentExitCodeError is not raised: the verifier still runs, as it
would after a real agent gave up). Tokens are zero.
"""
from __future__ import annotations

from pathlib import Path
import shlex
import tempfile
from typing import Any

from harbor.agents.base import BaseAgent
from harbor.agents.capabilities import AgentCapabilities
from harbor.agents.options import AgentOptions
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from pydantic import Field


class ScriptedOptions(AgentOptions):
    script: str = Field(default="true", description="The shell script to run in the environment.")
    shell: str = Field(default="bash", description="The shell that runs it.")
    timeout_sec: int | None = Field(default=600, description="The script's own time limit.")


class ScriptedAgent(BaseAgent):
    capabilities = AgentCapabilities()
    options_model = ScriptedOptions
    options: ScriptedOptions

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.exit_code: int | None = None

    @staticmethod
    def name() -> str:
        return "scripted"

    def version(self) -> str:
        return "1"

    async def setup(self, environment: BaseEnvironment) -> None:
        pass

    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        logs = self.environment_logs_dir
        remote = (logs / "instruction.md").as_posix()
        await environment.exec(command=f"mkdir -p {shlex.quote(logs.as_posix())}", user="root")
        with tempfile.TemporaryDirectory(prefix="hermit-scripted-") as tmp:
            local = Path(tmp) / "instruction.md"
            local.write_text(instruction)
            await environment.upload_file(local, remote)
        await environment.exec(command=f"chmod -R a+rwX {shlex.quote(logs.as_posix())}", user="root")
        result = await environment.exec(command=f"{self.options.shell} -c {shlex.quote(self.options.script)}",
                                        env={"HERMIT_INSTRUCTION_FILE": remote},
                                        timeout_sec=self.options.timeout_sec)
        self.exit_code = result.return_code
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        (self.logs_dir / "script.txt").write_text(f"$ {self.options.script}\n[exit {result.return_code}]\n"
                                                  f"--- stdout ---\n{result.stdout or ''}\n--- stderr ---\n"
                                                  f"{result.stderr or ''}\n")
        context.n_input_tokens = 0
        context.n_output_tokens = 0
        context.metadata = {**(context.metadata or {}), "exit_code": result.return_code}
