"""Full-MWM execution wiring, with the experiment's data isolation retained."""
from pathlib import Path
import json


def install():
    from harnessadapter.policy.execution import PolicySandbox
    if getattr(PolicySandbox, '_officeqa_full_installed', False): return
    original_init, original_fs = PolicySandbox.__init__, PolicySandbox.filesystem
    def initialize(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        # The checkout contains private test/evaluation directories. Shell
        # access needs the library package, never its whole checkout parent.
        from paths import REPOSITORY
        self.runtime_roots = [p for p in self.runtime_roots if p != REPOSITORY]
    def filesystem(self, *, writable=True):
        fs = original_fs(self, writable=writable)
        from harnessadapter.evolution.workspace import Workspace
        for name in (*Workspace(self.session).protected, '.git', 'state.json', 'AGENTS.md', 'CLAUDE.md'):
            fs[str(self.adapter / name)] = 'read'
        return fs
    PolicySandbox.__init__, PolicySandbox.filesystem = initialize, filesystem
    PolicySandbox._officeqa_full_installed = True
    from full_runtime import DockerPythonSandbox
    import harnessadapter.runtime.python_sandbox as python_sandbox
    python_sandbox.PythonSandbox = DockerPythonSandbox


if __name__ == '__main__':
    import sys
    install()
    from harnessadapter.evolution.server import create_server
    create_server(Path(sys.argv[1]).resolve()).run(transport='stdio')
