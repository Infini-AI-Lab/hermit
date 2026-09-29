"""MWM export bridge. The method keeps its original artifact structures and wm API."""
from pathlib import Path
import shutil

from .mwm_core import capabilities, delegate
from .mwm_core.wm_iface import WorldModelInterface

ROOT = Path(__file__).resolve().parent


class Interface(WorldModelInterface):
    def __init__(self, resources):
        self.evidence_bound = resources.get('evidence') is not None
        evidence = str(resources['evidence'].root) if self.evidence_bound else str(ROOT/'_no_evidence_binding')
        deployment = resources.get('deployment') or {}
        mode = deployment.get('delegation', 'native') if isinstance(deployment, dict) else 'native'
        # wm run has its own bubblewrap child (mwm_core/sandbox.py, the native
        # one); it does not depend on the framework's learned-tool sandbox.
        super().__init__(str(ROOT), evidence, delegation=mode if mode in ('native', 'none') else 'native',
                         code_channel=shutil.which('bwrap') is not None)

    def _trace_pointer(self, address):
        return super()._trace_pointer(address) if self.evidence_bound else ''

    def _run_tool(self, name, args):
        modules={s['name']:s['_module'] for s in capabilities.candidates(str(ROOT))}
        if name in modules:
            ok,reason = capabilities.run_test(str(ROOT),modules[name])
            if not ok:return f'{name}: not admitted — {reason}. Read tools/{modules[name]}/TOOL.md for the prose fallback.'
        return super()._run_tool(name,args)


def context(_resources=None):
    resources=_resources or {};token=capabilities.RESOURCES.set(resources)
    try:
        deployment=resources.get('deployment') or {}
        note=(deployment.get('transport_note') if isinstance(deployment,dict) else None) or 'The host exposes the single wm(name, args) tool through MCP. Native roles come from agents/<name>/SPEC.md; no role is seeded by default.'
        text=Interface(resources).package(note)
        sandbox=resources.get('python_sandbox')
        if sandbox is None or not sandbox.status()['available']:
            text+='\n\nExecution availability: the required sandbox is unavailable. Learned programs are not admitted or executed; use their TOOL.md prose fallback.'
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
    try:
        deployment=resources.get('deployment') or {}
        if name.strip().lower() == 'read' and isinstance(deployment,dict) and deployment.get('read_paging') is False:
            # Native behaviour: the first 4000 characters, no paging metadata.
            try:
                return {'text': Interface(resources).read_page(str(args.get('path','')), 0, 4000, None)['text']}
            except (ValueError, OSError) as exc:
                return {'text': f'read: {exc}'}
        if name.strip().lower() == 'read':
            try:
                return Interface(resources).read_page(
                    str(args.get('path', '')), args.get('offset', 0),
                    args.get('limit', 4000), args.get('expected_sha256'))
            except (ValueError, OSError) as exc:
                return {'text': f'read: {exc}', 'error': 'read_failed'}
        return {'text':Interface(resources).call(name,args)}
    finally:capabilities.RESOURCES.reset(token)
