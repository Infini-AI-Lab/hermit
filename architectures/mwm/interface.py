"""MWM export bridge. The method keeps its original artifact structures and wm API."""
from pathlib import Path

from .mwm_core import capabilities, delegate
from .mwm_core.wm_iface import WorldModelInterface

ROOT = Path(__file__).resolve().parent


class Interface(WorldModelInterface):
    def __init__(self, resources):
        self.evidence_bound = resources.get('evidence') is not None
        evidence = str(resources['evidence'].root) if self.evidence_bound else str(ROOT/'_no_evidence_binding')
        sandbox = resources.get('python_sandbox')
        super().__init__(str(ROOT), evidence, delegation='native',
                         code_channel=bool(sandbox and sandbox.status()['available']))

    def _trace_pointer(self, address):
        return super()._trace_pointer(address) if self.evidence_bound else ''

    def _run_tool(self, name, args):
        if name in {s['name'] for s in capabilities.candidates(str(ROOT))}:
            ok,reason = capabilities.run_test(str(ROOT),name)
            if not ok:return f'{name}: not admitted — {reason}. Read tools/{name}/TOOL.md for the prose fallback.'
        return super()._run_tool(name,args)


def context(_resources=None):
    resources=_resources or {};token=capabilities.RESOURCES.set(resources)
    try:
        text=Interface(resources).package('The host exposes the single wm(name, args) tool through MCP. Native roles come from agents/<name>/SPEC.md; no role is seeded by default.')
        sandbox=resources.get('python_sandbox')
        if sandbox is None or not sandbox.status()['available']:
            text+='\n\nExecution availability: the required sandbox is unavailable. Learned programs are not admitted or executed; use their TOOL.md prose fallback. wm run is unavailable.'
        return text
    finally:capabilities.RESOURCES.reset(token)


def agent_definitions():
    return [{'name':name,'description':description,'instructions':f'agents/{name}/SPEC.md'}
            for name,description in delegate.list_specs(str(ROOT))
            if (ROOT/'agents'/name/'SPEC.md').resolve().is_relative_to(ROOT)]


def wm(name: str, args: dict | None = None, _resources=None) -> dict:
    resources=_resources or {};args=args or {}
    if name.strip().lower()=='trace' and 'evidence' not in resources:
        return {'text':'trace: no evidence resource is bound for this deployment'}
    if name in ('memory','recall'):
        address=str(args.get('id' if name=='memory' else 'address',''))
        if '/' in address or '\\' in address or address.startswith('.'):
            return {'text':'invalid memory or record address'}
    token=capabilities.RESOURCES.set(resources)
    try:return {'text':Interface(resources).call(name,args)}
    finally:capabilities.RESOURCES.reset(token)
