"""A toy-pack v10 campaign root in the production layout ($0, synthetic data only).

- `code_copy(dest)`: a private copy of the plumbing sources the freeze hashes, whose `adapters/prompt-w0` and
  `adapters/harness-w0` are the W0 adapters built for the toy pack (`arms.build_w0`, i.e. `arms.py build --pack
  toy`); the harness W0's tools/ (lookup.py, corpus.py, calc.py, manifest.json, USAGE.md) are what the campaign
  serves to the actor from the root's neutral code root (campaign.stage_code_root), never packs/toy itself.
- `make_root(base, name, spec)`: `<root>/prepared` exactly as data.py lays it out (agent/corpus = the toy
  Widget Bulletin files, agent/corpus.sqlite = the same FTS5 index builder as OfficeQA, agent/tasks.jsonl,
  private/answers.jsonl = the toy references, private/corpus_manifest.json, manifest.json = data.manifest_v9).
  The toy pack has no tables index (campaign.uses_tables_index). One toy question can be declared disputed
  (`withheld`): its reference is never released to a learner but it is scored, like OfficeQA's issue-47 ids.
- `toy_score`: the toy pack's grader in the reward.py `score_answer(reference, predicted, tolerance=...)`
  shape; it stands in for the official scorer (Hooks.scorer), never for a model-visible tool.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil

from harness_core.numeric import numbers_in
from packs.toy import corpus as toy_corpus
from packs.toy.items import ITEMS, REFERENCES, grade

PLUMBING = Path(__file__).resolve().parents[2]


def code_copy(dest: Path) -> Path:
    """The frozen code of every toy root (shared by the repeat group)."""
    import arms
    from packs.toy.pack import ToyPack
    dest = Path(dest)
    dest.mkdir(parents=True)
    ignore = shutil.ignore_patterns('__pycache__', '.pytest_cache', '*.pyc')
    for path in PLUMBING.glob('*.py'):
        shutil.copyfile(path, dest / path.name)
    for name in ('PROTOCOL.md', 'PREREG.md', 'cards_domain.md'):
        if (PLUMBING / name).is_file():
            shutil.copyfile(PLUMBING / name, dest / name)
    if not (dest / 'PREREG.md').is_file():          # PREREG-v10 is written after S4: a v10-headed placeholder
        (dest / 'PREREG.md').write_text('# v10 pre-registration (toy E2E placeholder)\n\nThe real PREREG.md is '
                                        'written and hashed after S4 (DESIGN §7); the toy rehearsal freezes this one.\n')
    for name in ('harness_core', 'packs'):
        shutil.copytree(PLUMBING / name, dest / name, ignore=ignore)
    (dest / 'adapters').mkdir()
    for seed, name in arms.W0_NAMES.items():
        arms.build_w0(seed, ToyPack(), dest / 'adapters' / name)
    return dest


def toy_score(reference: str, predicted: str, tolerance: float = 0.0) -> float:
    """Strict = packs.toy.items.grade; 1% = same count of numbers, each within 1% of the reference."""
    if not tolerance:
        return float(grade(predicted or '', reference or ''))
    got, want = numbers_in(predicted or ''), numbers_in(reference or '')
    return float(bool(want) and len(got) == len(want)
                 and all(abs(g - w) <= tolerance * abs(w) + 1e-12 for g, w in zip(got, want)))


def make_root(base: Path, name: str, spec, *, withheld: tuple[str, ...] = ()) -> Path:
    """<base>/<name>/prepared for one arm root of the toy campaign (data.py layout)."""
    import data
    import experiment
    root = Path(base) / name
    prepared = root / 'prepared'
    corpus = toy_corpus.make_corpus(prepared / 'agent/corpus')
    meta = data._tools.build_index(corpus, prepared / 'agent/corpus.sqlite', expected_documents=len(toy_corpus.FILES))
    (prepared / 'index-metadata.json').write_text(json.dumps(meta, indent=2) + '\n')
    ids = [item.item_id for item in ITEMS]
    (prepared / 'agent/tasks.jsonl').write_text(''.join(json.dumps({'task_id': i.item_id, 'question': i.question}) + '\n'
                                                        for i in ITEMS))
    private = prepared / 'private'
    private.mkdir()
    (private / 'answers.jsonl').write_text(''.join(json.dumps({'task_id': i, 'answer': REFERENCES[i]}) + '\n' for i in ids))
    rows = [{'filename': p.name, 'sha256': data.sha256(p), 'bytes': p.stat().st_size} for p in sorted(corpus.glob('*.txt'))]
    (private / 'corpus_manifest.json').write_text(json.dumps(rows))
    base_manifest = {
        'selection': {'seed': 'toy-e2e'}, 'sample_count': len(ids), 'task_ids': ids,
        'tasks_sha256': data.sha256(prepared / 'agent/tasks.jsonl'),
        'answers_sha256': data.sha256(private / 'answers.jsonl'),
        'corpus_manifest_sha256': data.sha256(private / 'corpus_manifest.json'),
        'corpus_document_count': len(rows), 'index_sha256': data.sha256(prepared / 'agent/corpus.sqlite'),
        'index_metadata_sha256': data.sha256(prepared / 'index-metadata.json'),
        'grader': {'path': 'packs/toy/items.py:grade', 'note': 'toy rehearsal; the toy grader stands in for reward.py'},
        'fixture_only': True}
    manifest = data.manifest_v9(base_manifest, spec, tables={}, actor_seconds=experiment.ACTOR_SECONDS)
    # the toy's stand-in for databricks/officeqa#47: declared disputed, scored, never released to a learner
    manifest['learner_policy']['withheld_reference_ids'] = [i for i in ids if i in withheld]
    manifest['grading_policy']['subsets'] = {'all': len(ids), 'undisputed': len(ids) - len(withheld)}
    (prepared / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return root
