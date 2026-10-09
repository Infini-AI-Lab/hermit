"""Method-side binding to framework execution; no local execution fallback."""
from .capabilities import RESOURCES


def run_sandboxed(code, arguments):
    sandbox=RESOURCES.get().get('python_sandbox')
    if sandbox is None:return {'error':'No sandbox resource is bound'}
    result=sandbox.run_code(code,arguments)
    return result if result.get('passed') else {'error':result.get('output','Sandbox unavailable')}
