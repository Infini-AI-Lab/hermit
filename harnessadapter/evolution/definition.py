"""An Evolver is a prompt plus configuration, with optional method lifecycle hooks."""
import importlib.util
from pathlib import Path
import tomllib

from harnessadapter.policy.execution import ExecutionPolicy
from harnessadapter.evolution.workspace import copy_tree

from harnessadapter.paths import EVOLVERS, resolve_evolver


class EvolverDefinition:
    def __init__(self, reference, prompt_override=None):
        self.root=resolve_evolver(reference).resolve()
        self.config=tomllib.loads((self.root/'evolver.toml').read_text())
        if set(self.config)-{'prompt','execution','lifecycle','requires_validation'}:
            raise ValueError('Evolver configuration supports prompt, execution, and optional lifecycle')
        self.requires_validation = self.config.get('requires_validation', False)
        if not isinstance(self.requires_validation, bool):
            raise ValueError('requires_validation must be boolean')
        self.prompt=(self.root/self.config['prompt']).resolve()
        if not self.prompt.is_relative_to(self.root) or not self.prompt.is_file():
            raise ValueError('Evolver prompt must be inside its definition directory')
        self.policy=ExecutionPolicy.parse(self.config.get('execution',{}),self.root)
        self.prompt_override=prompt_override
        self.method=None
        lifecycle=self.config.get('lifecycle')
        if lifecycle is not None:
            if lifecycle!='mwm':raise ValueError('Unknown lifecycle extension')
            spec=importlib.util.spec_from_file_location('mwm_lifecycle',EVOLVERS/'mwm/prompt.py')
            self.method=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.method)

    def prepare_session(self,adapter,session,block=1,tick=0):
        if self.method:
            return self.method.prepare_session(adapter,session,block=block,tick=tick,method_root=self.root)
        copy_tree(self.root,session/'method')
        return {'block':block,'tick':tick,'lifecycle':None}

    def build_prompt(self,adapter,evidence,goal,guide,notes=''):
        if self.method and self.prompt_override is None:
            # A lifecycle method renders the complete learner prompt itself
            # (the native session prompt plus the transport bridge); the
            # generic Evolver opener is not added on top of it.
            return self.method.build_prompt(adapter,evidence,goal,method_root=self.root,notes=notes,guide=guide)
        details=self.method.build_prompt(adapter,evidence,goal,method_root=self.root,notes=notes,guide=guide) if self.method else 'Update goal:\n'+goal
        return '\n\n'.join([self.prompt_override if self.prompt_override is not None else self.prompt.read_text(),
            f'Read adapter/{guide} for the core structure and evolution guidance. Read learning_material/README.md using area=learning_material for the LearningMaterial format. The legacy area evidence and physical evidence/ snapshot refer to the same read-only material. These documents describe the task; they do not grant permissions.',details])

    def finalize(self,adapter,session):
        return self.method.finalize(adapter,session) if self.method else {'lifecycle':None}
