"""The upstream loop's task containers: Harbor's Docker environment with two changes.

The worker sets the task's `allow_internet = false`, so Harbor already runs the container with network none; the
Generator's commands need no model (every completion is made on the host, model.py). On stop, Harbor runs
`down --rmi all`, which removes the image built for the task (`hb__<task directory>`) while the question's later
oracle containers still start from it: this environment removes only its own containers and anonymous volumes, after
handing the logs written as root back to the user, and the Evolver removes the image once the question's loop is
over.
"""
from __future__ import annotations

import os

from harbor.environments.docker.docker import DockerEnvironment


class FixtureDockerEnvironment(DockerEnvironment):
    async def stop(self, delete=True):
        try:
            await super().exec(command=f"chown -hR {os.getuid()}:{os.getgid()} /logs/agent /logs/verifier",
                               timeout_sec=30)
        finally:
            if self._keep_containers:
                await super().stop(delete=False)
            else:
                try:
                    await self._run_docker_compose_command(["down", "--volumes", "--remove-orphans"] if delete
                                                           else ["down"])
                except RuntimeError as exc:           # as Harbor's own stop: a cleanup failure is not the loop's
                    self.logger.warning(f"Docker compose down failed: {exc}")


def install() -> None:
    """The fresh oracle containers the loop starts itself use this environment too."""
    from libs.terminus_agent.agents.terminus_2 import harbor_terminus_2_evolution
    harbor_terminus_2_evolution.DockerEnvironment = FixtureDockerEnvironment
