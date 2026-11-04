"""An indexed, read-only view of complete observable TRAIN experience.

Only explicitly registered training rows and known public artifacts enter this
store. Role tools accept document IDs, never filesystem paths. Large observations
are paginated, not permanently clipped to fit a role's first prompt.
"""
import hashlib
import json
from pathlib import Path

from hermit.agent.base.tool import Tool
from .evidence import content_text, read_messages


class TrainingExperience:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / 'index.json'
        self.index = json.loads(self.index_path.read_text()) if self.index_path.exists() else {}
        if any(d.get('split') != 'train' for d in self.index.values()):
            raise ValueError('experience store must contain TRAIN documents only')

    def put(self, document_id, value, *, kind, **metadata):
        if set(metadata) & {'document_id', 'split', 'kind', 'chars', 'sha256'}:
            raise ValueError('reserved experience metadata')
        text = json.dumps(value, ensure_ascii=False, indent=2)
        digest = hashlib.sha256(text.encode()).hexdigest()
        (self.root / (digest + '.json')).write_text(text)
        self.index[document_id] = {'document_id': document_id, 'split': 'train',
            'kind': kind, 'chars': len(text), 'sha256': digest, **metadata}
        self.index_path.write_text(json.dumps(self.index, ensure_ascii=False, indent=2))

    def episodes(self, label, rows, train_ids):
        """Copy complete observable logs from returned TRAIN paths, without discovery.

        The caller supplies its current training IDs. Validation rows cannot be
        registered even if their log happens to exist on the machine.
        """
        if any(row['id'] not in train_ids for row in rows):
            raise ValueError('cannot register non-TRAIN outcomes')
        for row in rows:
            prefix = f"{label}/case/{row['id']}/repeat-{row.get('repeat', 1)}"
            meta = {'case_id': row['id'], 'repeat': row.get('repeat', 1), 'version': label}
            outcome = {k: row[k] for k in ('id', 'repeat', 'status', 'output', 'judge') if k in row}
            self.put(prefix + '/outcome', outcome, kind='outcome', **meta)
            messages = row.get('messages')
            directory = None
            if row.get('log'):
                path = Path(row['log'])
                directory = path if path.is_dir() else path.parent
                path = directory / 'trajectory.json' if path.is_dir() else path
                logged = read_messages(path) if path.is_file() else None
                if logged is not None:
                    messages = logged
            if messages is not None:
                # Keep complete observable content and arguments; private provider
                # reasoning is not evidence for the defensive roles.
                observable = [{k: m[k] for k in ('role', 'tool_calls', 'tool_call', 'tool_call_id', 'error', 'name')
                               if k in m}
                              | {'message_index': n, 'content': content_text(m.get('content'), m.get('role'))}
                              for n, m in enumerate(messages)]
                self.put(prefix + '/trajectory', observable, kind='trajectory', **meta)
            if directory is not None:
                for filename, kind in [('audit.json', 'hook_audit'),
                                       ('llm-auditor-calls.jsonl', 'classifications')]:
                    path = directory / filename
                    if path.is_file():
                        value = ([json.loads(line) for line in path.read_text().splitlines() if line.strip()]
                                 if filename.endswith('.jsonl') else json.loads(path.read_text()))
                        self.put(prefix + '/' + kind, value, kind=kind, **meta)

    def catalogue(self, query='', offset=0, limit=40):
        query = query.casefold()
        matches = [d for d in self.index.values() if query in json.dumps(d, ensure_ascii=False).casefold()]
        return {'documents': matches[offset:offset + limit], 'total': len(matches),
                'next_offset': offset + limit if offset + limit < len(matches) else None}

    def read(self, document_id, offset=0, chars=16000):
        record = self.index[document_id]
        digest = record['sha256']
        if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('invalid experience content hash')
        path = self.root / (digest + '.json')
        if path.is_symlink():
            raise ValueError('experience content must not be a symlink')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError('experience content hash mismatch')
        text = raw.decode()
        end = min(len(text), offset + chars)
        return {'document': record, 'offset': offset, 'text': text[offset:end],
                'next_offset': end if end < len(text) else None}

    def brief(self, *, interactive=True):
        return {'scope': 'TRAIN only; complete observable trajectories, audits, source, reviews and lessons',
                'documents': len(self.index),
                'case_ids': sorted({d['case_id'] for d in self.index.values() if 'case_id' in d}),
                'versions': sorted({d['version'] for d in self.index.values() if 'version' in d}),
                'tools': ['list_training_experience', 'read_training_experience'] if interactive and self.index else [],
                'instructions': ('Use the catalogue to find cases/versions/kinds, then read every needed page. '
                                 'No sample-count cap; a truncated initial summary is not missing experience.'
                                 if interactive and self.index else 'No interactive TRAIN reader in this call; use supplied evidence.')}

    def tools(self, *, log=None, cancel=None):
        paging = {'offset': {'type': 'integer', 'minimum': 0}}
        tools = [Tool('list_training_experience',
            'List registered TRAIN experience; query matches case IDs, versions and artifact kinds. '
            'Paginate until next_offset is null. Does not scan project files.', self.catalogue,
            source='run', input_schema={'type': 'object', 'additionalProperties': False, 'properties': {
                'query': {'type': 'string'}, **paging, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}}}),
            Tool('read_training_experience',
            'Read a complete TRAIN document by its catalogue ID, including full observable trajectories, '
            'hook audits or candidate source. Continue at next_offset to read beyond this page.', self.read,
            source='run', input_schema={'type': 'object', 'additionalProperties': False,
                'required': ['document_id'], 'properties': {'document_id': {'type': 'string'}, **paging,
                    'chars': {'type': 'integer', 'minimum': 1, 'maximum': 32000}}})]
        for tool in tools:
            tool.log_path, tool.cancel = log, cancel
        return tools


def role_tools(ctx):
    state = getattr(ctx.workspace, 'state', None)
    if state is None or not (state / 'training-experience' / 'index.json').is_file():
        return ()
    return TrainingExperience(state / 'training-experience').tools(
        log=ctx.workspace.output / 'training-experience-access.jsonl', cancel=ctx.cancel)
