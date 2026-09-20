"""Animation-making experience: methods, adaptable code, and working examples."""
import json
from pathlib import Path
from harnessadapter.runtime.source_text import read_text_page

ROOT = Path(__file__).resolve().parent


def entries():
    result = json.loads((ROOT / 'index.json').read_text())
    if not isinstance(result, list):
        raise ValueError('index.json must be a list')
    seen = set()
    for entry in result:
        if entry.get('kind') not in ('method', 'implementation', 'example'):
            raise ValueError('Unknown experience kind')
        if not entry.get('id') or entry['id'] in seen:
            raise ValueError('Entry ids must be unique')
        seen.add(entry['id'])
        for key in ('title', 'summary', 'path', 'scope'):
            if not isinstance(entry.get(key), str) or not entry[key].strip():
                raise ValueError('Entries need title, summary, path and scope')
        read_text_page(ROOT, entry['path'], limit=1)
    return result


def context(_resources=None):
    guide = (ROOT / 'ADAPTER.md').read_text()
    brief = (ROOT / 'briefing.md').read_text()
    preferences = (ROOT / 'preferences.md').read_text()
    catalog = '\n'.join(f"- [{e['id']}] {e['kind']}: {e['summary']} | scope: {e['scope']} | {e['path']}" for e in entries()) or '(No learned entries yet.)'
    return f'{guide}\n\n## Current guidance\n{brief}\n\n## Scoped preferences\n{preferences}\n\n## Available experience\n{catalog}\n'


def workshop(name, args=None, _resources=None):
    args = args or {}
    if name == 'list':
        return {'entries': entries(), 'root_files': read_text_page(ROOT, '')['text']}
    if name == 'read':
        return read_text_page(ROOT, args.get('path', ''), args.get('offset', 0),
                              args.get('limit', 6000), args.get('expected_sha256'))
    if name == 'search':
        query = args.get('query', '')
        if not isinstance(query, str) or not query:
            raise ValueError('Provide a nonempty query')
        matches = []
        for folder in ('methods', 'implementations', 'examples'):
            for path in sorted((ROOT / folder).rglob('*')):
                if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(ROOT):
                    continue
                try: lines = path.read_text().splitlines()
                except (UnicodeError, OSError): continue
                for line, text in enumerate(lines, 1):
                    if query.lower() in text.lower():
                        matches.append({'path': str(path.relative_to(ROOT)), 'line': line, 'text': text[:240]})
                        if len(matches) == 30: return {'matches': matches, 'more_possible': True}
        return {'matches': matches, 'more_possible': False}
    raise ValueError('Use list, read or search. Source is for local construction, not remote runtime execution.')
