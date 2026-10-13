"""MWM tool bundles and admission policy; execution is a framework resource.

Never imports tools/<name>/run.py. Only statically read literal SCHEMA values.
"""
import ast
from contextvars import ContextVar
import hashlib
import json
from pathlib import Path

RESOURCES = ContextVar('mwm_resources', default={})
_CACHE = {}


def _entries(wrepo):
    root=Path(wrepo).resolve();out=[]
    for path in sorted((root/'tools').glob('*/run.py')):
        if path.parent.name.startswith(('_','.')) or not path.resolve().is_relative_to(root):continue
        out.append((path.parent.name,str(path)))
    return out


def candidates(wrepo):
    out=[]
    for directory,path in _entries(wrepo):
        try:
            tree=ast.parse(Path(path).read_text());schema=None;has_run=False
            for node in tree.body:
                if isinstance(node,ast.Assign) and any(getattr(t,'id',None)=='SCHEMA' for t in node.targets):schema=ast.literal_eval(node.value)
                if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name=='run':has_run=True
            if isinstance(schema,dict) and schema.get('name')==directory and has_run:
                out.append({**schema,'_module':directory})
        except (OSError,SyntaxError,ValueError,TypeError):continue
    return out


def _fingerprint(root):
    digest=hashlib.sha256()
    for p in sorted(root.rglob('*')):
        if not p.is_file() or p.is_symlink() or any(x in ('.git','__pycache__') for x in p.relative_to(root).parts):continue
        digest.update(str(p.relative_to(root)).encode());digest.update(p.read_bytes())
    return digest.hexdigest()


def run_test(wrepo,module):
    sandbox=RESOURCES.get().get('python_sandbox')
    if sandbox is None or not sandbox.status()['available']:
        return False,'unavailable: Linux bubblewrap sandbox is not bound or available'
    root=Path(wrepo).resolve();directory=root/'tools'/module
    if not (directory/'test.py').is_file() or not (directory/'test.py').resolve().is_relative_to(root):
        return False,'missing or out-of-repo test.py'
    key=(str(root),module,_fingerprint(root),id(sandbox))
    if key not in _CACHE:
        result=sandbox.test(f'tools/{module}')
        _CACHE[key]=(result['passed'],result.get('output',''))
    return _CACHE[key]


def discover(wrepo):
    return [schema for schema in candidates(wrepo) if run_test(wrepo,schema['_module'])[0]]


def invoke(wrepo,module,kwargs):
    ok,diagnostic=run_test(wrepo,module)
    if not ok:return 'tool not admitted: '+diagnostic
    result=RESOURCES.get()['python_sandbox'].invoke(f'tools/{module}',kwargs)
    return result.get('stdout','').strip() if result['passed'] else 'tool execution failed: '+result.get('output','')
