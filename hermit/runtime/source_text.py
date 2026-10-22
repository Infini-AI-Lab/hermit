"""Bounded, explicit source pagination for adapter-owned text artifacts."""
import hashlib
from pathlib import Path


def read_text_page(root, path, offset=0, limit=6000, expected_sha256=None):
    root = Path(root).resolve()
    if not isinstance(path, str) or Path(path).is_absolute() or '..' in Path(path).parts or '.git' in Path(path).parts:
        raise ValueError('Use a relative adapter path without traversal or .git')
    if type(offset) is not int or offset < 0:
        raise ValueError('offset must be a nonnegative integer')
    if type(limit) is not int or not 1 <= limit <= 24000:
        raise ValueError('limit must be between 1 and 24000 characters')
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        raise ValueError('Path escapes the adapter')
    if target.is_dir():
        raw = '\n'.join(sorted(p.name + ('/' if p.is_dir() else '') for p in target.iterdir()
                               if not p.name.startswith('.') and p.resolve().is_relative_to(root))).encode()
        kind = 'directory'
    elif target.is_file():
        raw, kind = target.read_bytes(), 'file'
    else:
        raise ValueError('No such adapter file or directory')
    text = raw.decode('utf-8')
    digest = hashlib.sha256(raw).hexdigest()
    if expected_sha256 is not None and expected_sha256 != digest:
        raise ValueError('Content changed; restart at offset 0')
    if offset > len(text):
        raise ValueError('offset exceeds total_chars')
    end = min(offset + limit, len(text))
    return {'path': path, 'kind': kind, 'text': text[offset:end], 'offset': offset,
            'total_chars': len(text), 'next_offset': end if end < len(text) else None,
            'eof': end == len(text), 'sha256': digest}
