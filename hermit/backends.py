"""Moved to hermit.agent.model.backends."""
from hermit._moved import forward

if __name__ == "__main__":                 # `python -m hermit.backends ...`, as older launchers run it
    import runpy
    runpy.run_module("hermit.agent.model.backends", run_name="__main__", alter_sys=True)
else:
    forward(__name__, "hermit.agent.model.backends")
