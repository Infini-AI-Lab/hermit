"""File capabilities for a learner. Candidate code is never executed here."""
import ast
import hashlib
import json
from pathlib import Path
import shutil

from harnessadapter.adapter.loader import load_manifest, inside


def fingerprint(root: Path, exclude=()):
    """Content hashes of every regular file under root; `exclude` names
    top-level entries to leave out (the bundled methods and architectures
    when fingerprinting the framework code itself)."""
    skip = {'.git', '__pycache__', '.venv', '.venv-demo'}
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob('*')) if p.is_file() and not p.is_symlink()
            and not any(x in skip for x in p.relative_to(root).parts)
            and p.relative_to(root).parts[0] not in exclude}


def copy_tree(source: Path, target: Path):
    for path in source.rglob('*'):
        relative = path.relative_to(source)
        if any(x in {'.git', '__pycache__', '.venv', '.venv-demo'} for x in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError(f'Symlink not admitted to this initial evolution workspace: {relative}')
        if path.is_file():
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
    target.mkdir(parents=True, exist_ok=True)


class Workspace:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.areas = {name: self.root / name for name in ('adapter', 'evidence', 'scratch', 'method', 'environment', 'history')}

        if (self.root / 'validation').is_dir():
            self.areas['validation'] = self.root / 'validation'

    def path(self, area, relative, write=False):
        if area not in self.areas or (write and area not in ('adapter', 'scratch')):
            raise ValueError('Read: adapter/evidence/scratch/method/environment. Write: adapter/scratch only.')
        if not isinstance(relative, str) or Path(relative).is_absolute() or '..' in Path(relative).parts or '.git' in Path(relative).parts:
            raise ValueError('Use a relative path without parent traversal or .git')
        base = self.areas[area].resolve()
        result = (base / relative).resolve()
        if not result.is_relative_to(base):
            raise ValueError('Path escapes the granted area')
        if write and result == base:
            raise ValueError('An area root cannot be replaced or removed')
        return result

    def list_files(self, area):
        base = self.path(area, '')
        return {'files': sorted(str(p.relative_to(base)) for p in base.rglob('*') if p.is_file()
                                and not p.is_symlink() and p.resolve().is_relative_to(base))}

    def read_file(self, area, path, offset=0):
        file = self.path(area, path)
        if offset < 0:
            raise ValueError('offset must be nonnegative')
        content = file.read_text()
        end = min(offset + 30000, len(content))
        return {'area': area, 'path': path, 'text': content[offset:end],
                'next_offset': end if end < len(content) else None}

    def write_file(self, area, path, content):
        file = self.path(area, path, write=True)
        if len(content.encode()) > 1000000:
            raise ValueError('One write is limited to 1 MB')
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content)
        return {'written': path, 'area': area, 'bytes': len(content.encode())}

    def delete_path(self, area, path):
        file = self.path(area, path, write=True)
        if file.is_dir():
            if any(file.iterdir()):
                raise ValueError('Delete files explicitly before removing a nonempty directory')
            file.rmdir()
        else:
            file.unlink()
        return {'deleted': path, 'area': area}

    def move_path(self, area, source, destination):
        src, dst = self.path(area, source, write=True), self.path(area, destination, write=True)
        if dst.exists():
            raise ValueError('Destination already exists')
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        return {'moved': source, 'to': destination, 'area': area}

    def validate(self):
        from jsonschema import Draft202012Validator
        root = self.areas['adapter']
        findings = []
        try:
            manifest = load_manifest(root)
            for tool in manifest.get('tools', []):
                for key in ('input_schema', 'output_schema'):
                    Draft202012Validator.check_schema(tool[key])
                module, function = tool['entrypoint'].split(':', 1)
                tree = ast.parse(inside(root, module).read_text())
                if not any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == function for n in tree.body):
                    findings.append(f'Entrypoint must be a top-level Python function: {tool["entrypoint"]}')
            if manifest.get('agents'):
                declaration = manifest['agents']
                # A dynamic exporter is checked statically here, never executed.
                if isinstance(declaration, dict):
                    module, function = declaration['entrypoint'].split(':', 1)
                    tree = ast.parse(inside(root, module).read_text())
                    if not any(isinstance(n, ast.FunctionDef) and n.name == function for n in tree.body):
                        findings.append('Missing agent exporter function')
                    roles = []
                else:
                    roles = json.loads(inside(root, declaration).read_text())['agents']
                names = []
                for role in roles:
                    if not role.get('name') or not role.get('description'):
                        findings.append('Each role needs a name and description')
                    inside(root, role['instructions'])
                    names.append(role['name'])
                if len(names) != len(set(names)):
                    findings.append('Duplicate role names')
            for p in root.rglob('*.py'):
                ast.parse(inside(root, str(p.relative_to(root))).read_text())
        except Exception as exc:
            findings.append(f'{type(exc).__name__}: {exc}')
        return {'passed': not findings, 'findings': findings,
                'scope': 'Static contract and Python syntax only. No candidate code executed; no performance claim.'}

    def test_python(self, directory):
        from harnessadapter.runtime.python_sandbox import PythonSandbox
        self.path('adapter', directory)
        return PythonSandbox(self.areas['adapter']).test(directory)
