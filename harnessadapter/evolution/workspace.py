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
        self.areas = {name: self.root / name for name in ('adapter', 'evidence', 'scratch', 'method', 'environment', 'history', 'observations')}

        self.areas['learning_material'] = self.areas['evidence']

        if (self.root / 'validation').is_dir():
            self.areas['validation'] = self.root / 'validation'
        if (self.root / 'deployment').is_dir():
            self.areas['deployment'] = self.root / 'deployment'
        # Adapter paths the evolver definition declares immutable (its core
        # code); the session writes control/protected.json before the broker
        # starts, and finalize refuses a candidate whose protected files changed.
        self.protected = ('CONTRACT.md',)
        listing = self.root / 'control/protected.json'
        if listing.is_file():
            self.protected = tuple(dict.fromkeys(('CONTRACT.md', *json.loads(listing.read_text()))))
        # Environment mounts (control/mounts.json): host paths readable at
        # environment/<name>, never writable, never copied.
        self.mounts = {}
        mounts = self.root / 'control/mounts.json'
        if mounts.is_file():
            self.mounts = {k: Path(v) for k, v in json.loads(mounts.read_text()).items()}

    # The native learner's mounts, as its contract names them, mapped onto
    # the areas this transport serves: "/skills/curate-brief" is the method
    # area's skills/curate-brief, "/evidence/README.md" the evidence area's
    # README.md. The texts stay native; the resolver does the translation.
    MOUNTS = {'evidence': ('evidence', ''), 'skills': ('method', 'skills'), 'patterns': ('method', 'patterns'),
              'environment': ('environment', ''), 'world-model': ('adapter', '')}

    def mount(self, path):
        """(area, relative) for a native mount path such as /skills/x or
        skills/x, or None when `path` names no mount."""
        parts = Path(str(path)).parts
        if parts and parts[0] == '/':
            parts = parts[1:]
        # "../evidence/x" from the adapter's own directory is the same mount.
        while parts and parts[0] == '..':
            parts = parts[1:]
        if not parts or parts[0] not in self.MOUNTS:
            return None
        area, prefix = self.MOUNTS[parts[0]]
        if area not in self.areas:
            return None
        rest = Path(*parts[1:]) if len(parts) > 1 else Path('')
        relative = str(Path(prefix) / rest) if prefix else str(rest)
        return area, ('' if relative == '.' else relative)

    def path(self, area, relative, write=False):
        if not write and isinstance(relative, str) and (relative.startswith(('/', '..')) or (area in self.areas and not (self.areas[area] / relative).exists())):
            mapped = self.mount(relative)
            if mapped is not None:
                area, relative = mapped
        if area not in self.areas or (write and area not in ('adapter', 'scratch')):
            raise ValueError('Read: adapter/learning_material (legacy evidence)/scratch/method/environment/history/deployment. Write: adapter/scratch only.')
        if not isinstance(relative, str) or Path(relative).is_absolute() or '..' in Path(relative).parts or '.git' in Path(relative).parts:
            raise ValueError('Use a relative path without parent traversal or .git')
        if write and area == 'adapter' and self.protected_hit(relative):
            raise ValueError(f'{self.protected_hit(relative)} is protected core of this architecture; it is not editable')
        base = self.areas[area].resolve()
        parts = Path(relative).parts if isinstance(relative, str) else ()
        if area == 'environment' and parts and parts[0] in self.mounts:
            if write:
                raise ValueError('Environment mounts are read-only')
            target = self.mounts[parts[0]].resolve()
            result = (target / Path(*parts[1:])).resolve() if len(parts) > 1 else target
            if not (result == target or result.is_relative_to(target)):
                raise ValueError('Path escapes the mount')
            return result
        result = (base / relative).resolve()
        if not result.is_relative_to(base):
            raise ValueError('Path escapes the granted area')
        if write and result == base:
            raise ValueError('An area root cannot be replaced or removed')
        return result

    def protected_hit(self, relative):
        """The protected entry `relative` falls under, or None."""
        parts = Path(relative).parts
        for entry in self.protected:
            entry_parts = Path(entry).parts
            if parts[:len(entry_parts)] == entry_parts:
                return entry
        return None

    def protected_files(self, hashes):
        """The subset of a fingerprint that lies under a protected entry."""
        return {path: digest for path, digest in hashes.items() if self.protected_hit(path)}

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
            context = manifest['context']
            if isinstance(context, dict):
                module, function = context['entrypoint'].split(':', 1)
                tree = ast.parse(inside(root, module).read_text())
                if not any(isinstance(n, ast.FunctionDef) and n.name == function for n in tree.body):
                    findings.append(f'Context renderer must be a top-level Python function: {context["entrypoint"]}')
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
