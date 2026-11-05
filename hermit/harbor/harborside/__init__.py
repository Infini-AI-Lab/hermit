"""The half of Hermit's Harbor support that runs under Harbor's own interpreter: imported by `harbor run` (the
container-side agents) or started by the stream client (the driver). Harbor lives in its own venv, so nothing here
imports `hermit`: harbor and the standard library only. Hermit puts this directory's parent on Harbor's PYTHONPATH.

    harborside.codex:HermitCodex      Harbor's Codex with a Hermit adapter deployed into the task container
    harborside.scripted:ScriptedAgent a shell script as the agent (smoke tests of task sets, no model)
    harborside.driver                 one multi-step task, run a step at a time on request (a stream)
"""
