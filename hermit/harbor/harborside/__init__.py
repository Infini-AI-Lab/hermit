"""The half of Hermit's Harbor support that runs under Harbor's own interpreter: imported by `harbor run` (the agents)
or started by the stream client (the driver). Harbor lives in its own venv, so nothing here imports `hermit`: harbor,
two of Harbor's own dependencies (pydantic, which its agents are built on; certifi, where it is installed, for
Codex's CA bundle of last resort) and the standard library only. Hermit puts this directory's parent on Harbor's
PYTHONPATH.

Every acting CLI runs inside the task container from the launch plan Hermit renders for its trial
(hermit.harbor.launch writes it; harborside.hermit carries it out the same way for each CLI):

    harborside.hermit                       the launch plan's executor: HermitInstalled, mixed in over Harbor's agent
    harborside.codex:HermitCodex            Codex (Harbor's install, or a mounted codex_bin with its rg and CA bundle)
    harborside.claude:HermitClaude          Claude Code (Harbor's install, or a mounted claude_bin; the subscription's
                                            token by descriptor)
    harborside.pi:HermitPi                  Pi (Harbor's npm install, or a mounted pi_bin)
    harborside.opencode:HermitOpenCode      OpenCode 2.2 (its GitHub release, musl or glibc, or a mounted opencode_bin)
    harborside.dsh:HermitDsh                DeepSeek Harness (Node 24 by nvm and npm, or a mounted dsh_bin)
    harborside.mini_swe_agent:HermitMiniSweAgent   mini-swe-agent (Harbor's uv install), running Hermit's runner from
                                            the plan's bundle (HermitLaunchError when that cannot run there: no other
                                            agent in its place)
    harborside.answer:HermitAnswer          an agent that only answers: one request to Hermit's model from Harbor's
                                            process, its answer written into the container
    harborside.scripted:ScriptedAgent       a shell script as the agent (smoke tests of task sets, no model)
    harborside.driver                       one multi-step task, run a step at a time on request (a stream), each step
                                            with its own launch plan
"""
