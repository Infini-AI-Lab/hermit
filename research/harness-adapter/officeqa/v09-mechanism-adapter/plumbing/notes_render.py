"""Scoped notes and the per-episode AGENTS.md (DESIGN §2 L5; engineering review #9).

A note is `adapters/<rev>/notes/<id>.json` (contracts.NoteSpec): keyword triggers (no regex), text
≤ NOTE_MAX bytes, the block it was admitted in. For each episode at most NOTES_PER_EPISODE notes whose
triggers match the question are rendered into AGENTS.md, chosen *before* the workspace marker hash is
written, in a deterministic order (admitted_block, then id). The chosen ids go into the marker
(`marker_extra['notes']`, unreadable to the actor) and full_audit re-derives them with `choose_ids`.

Trigger matching is `harness_core.engine.trigger_match`, the same function the rule engine uses: '*' or
an empty list matches every question; otherwise some trigger's word sequence must occur contiguously in
the question's words (lowercase alphanumerics), so 'public debt' matches "...the public debt..." but
not "...debt of the public...". Keyword triggers cannot backtrack (the cards learner's regexes could).

AGENTS.md = COMMON (identical for every arm) + the adapter's SEED.md (text arms: the full seed text;
mech arms: the minimal finishing text) + a "Notes for this question" section when a note was chosen.
The plain arm gets COMMON only.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Sequence

from harness_core import contracts as C
from harness_core.engine import trigger_match

NOTE_ID = re.compile(r'^[a-z][a-z0-9_]{1,47}$')
TRIGGER = re.compile(r'^(\*|[a-z0-9][a-z0-9 \-/.&]{0,59})$')
MAX_TRIGGERS = 12
NOTES_HEADER = '## Notes for this question'


def validate_note(note: C.NoteSpec) -> list[str]:
    errors = []
    if not isinstance(note.id, str) or not NOTE_ID.match(note.id):
        errors.append('id must match ^[a-z][a-z0-9_]{1,47}$')
    if (not isinstance(note.triggers, list) or len(note.triggers) > MAX_TRIGGERS
            or not all(isinstance(t, str) and TRIGGER.match(t) for t in note.triggers)):
        errors.append(f'triggers: ≤ {MAX_TRIGGERS} lowercase keywords/phrases (no regex), or "*"')
    if not isinstance(note.text, str) or not note.text.strip():
        errors.append('text required')
    elif len(note.text.encode('utf-8')) > C.NOTE_MAX:
        errors.append(f'text exceeds {C.NOTE_MAX} bytes')
    elif any(ord(ch) < 32 and ch not in '\n\t' for ch in note.text):
        errors.append('text contains control characters')
    if isinstance(note.admitted_block, bool) or not isinstance(note.admitted_block, int) or note.admitted_block < 0:
        errors.append('admitted_block must be a non-negative integer')
    if note.origin not in ('seed', 'learned'):
        errors.append('origin must be seed or learned')
    if not isinstance(note.stats, dict):
        errors.append('stats must be an object')
    return errors


def load_notes(adapter: Path | None) -> list[C.NoteSpec]:
    """notes/<id>.json of an adapter (file name = id), validated; [] for no adapter / no notes dir."""
    if adapter is None:
        return []
    directory = Path(adapter) / 'notes'
    if not directory.exists():
        return []
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError('notes/ must be a real directory')
    out = []
    for path in sorted(directory.glob('*.json')):
        if path.is_symlink() or not path.is_file():
            raise ValueError(f'note file must be a regular file: {path.name}')
        data = json.loads(path.read_text(encoding='utf-8'))
        names = set(C.NoteSpec.__dataclass_fields__)
        if not isinstance(data, dict) or set(data) - names or not {'id', 'triggers', 'text'} <= set(data):
            raise ValueError(f'{path.name}: a note has the NoteSpec fields {sorted(names)}')
        note = C.NoteSpec(**data)
        errors = validate_note(note)
        if errors:
            raise ValueError(f'{path.name}: {errors}')
        if path.stem != note.id:
            raise ValueError(f'note file {path.name} holds note {note.id}')
        out.append(note)
    return out


def choose(notes: Sequence[C.NoteSpec], question: str, limit: int = C.NOTES_PER_EPISODE) -> list[C.NoteSpec]:
    """≤ limit notes whose triggers match the question; order (admitted_block, id)."""
    matched = [n for n in notes if trigger_match(question, n.triggers)]
    return sorted(matched, key=lambda n: (n.admitted_block, n.id))[:limit]


def choose_ids(adapter: Path | None, question: str) -> list[str]:
    """What full_audit re-derives for an episode: the ids `compose_agents` recorded."""
    return [n.id for n in choose(load_notes(adapter), question)]


def render_note(note: C.NoteSpec) -> str:
    """Model-visible form of one note (== learner_common.render_note; what the text budget counts)."""
    return '- ' + ' '.join(note.text.split()) + '\n'


def render_agents(common: str, seed: str = '', notes: Sequence[C.NoteSpec] = ()) -> str:
    parts = [common.rstrip() + '\n']
    if seed.strip():
        parts.append('\n' + seed.strip() + '\n')
    if notes:
        parts.append('\n' + NOTES_HEADER + '\n\n' + ''.join(render_note(n) for n in notes))
    return ''.join(parts)


def compose_agents(common: str, adapter: Path | None, question: str) -> tuple[str, list[str], dict]:
    """(AGENTS.md text, chosen note ids, byte accounting) for one episode."""
    seed = (Path(adapter) / 'SEED.md').read_text(encoding='utf-8') if adapter is not None else ''
    notes = choose(load_notes(adapter), question)
    text = render_agents(common, seed, notes)
    info = {'agents_md_bytes': len(text.encode('utf-8')), 'common_bytes': len(common.rstrip().encode('utf-8')) + 1,
            'seed_bytes': len(seed.strip().encode('utf-8')) if seed.strip() else 0,
            'notes_bytes': sum(len(render_note(n).encode('utf-8')) for n in notes),
            'notes_shown': len(notes)}
    return text, [n.id for n in notes], info


def save_note(adapter: Path, note: C.NoteSpec) -> Path:
    """Write notes/<id>.json (tests and learners)."""
    errors = validate_note(note)
    if errors:
        raise ValueError(errors)
    path = Path(adapter) / 'notes' / f'{note.id}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(note.__dict__, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    return path
