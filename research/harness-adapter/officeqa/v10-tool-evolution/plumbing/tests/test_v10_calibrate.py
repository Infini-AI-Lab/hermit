"""calibrate.py (review fix: the calibration round PREREG requires had no entry point): a finished root's
scored episodes become one evidence block in a scratch root, the harness learner runs on it with the campaign's
ToolRunner and a capped verification runner, and preflight/calibration.json records proposals, screens,
replays, admission, actor runs and cost. Offline and $0: the toy campaign root of test_v10_learners, a scripted
model and a fake actor runner."""
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import calibrate as CAL  # noqa: E402
import evidence as EV  # noqa: E402
import learner_common as LC  # noqa: E402
import preflight  # noqa: E402
from packs.toy import PACK, REFERENCES  # noqa: E402
from packs.toy.corpus import make_corpus  # noqa: E402
from packs.toy.sim import QUESTIONS  # noqa: E402
from fake_learner_model import ScriptedModel, say  # noqa: E402
from test_v10_learners import (FakeActor, GRADER, ITEMS, harness_root, harness_session, patched, refs_of,  # noqa: E402,F401
                               runner_factory)

pytestmark = pytest.mark.skipif(not Path('/usr/bin/sandbox-exec').exists(), reason='macOS sandbox-exec required')


def references_of(root):
    tasks = {}
    for base in sorted((root / 'runs/adapted').glob('[0-9][0-9][0-9]')):
        task = json.loads((base / 'task.json').read_text())
        tasks[task['task_id']] = REFERENCES[ITEMS[int(base.name) - 1]]
    return tasks


def test_scratch_root_is_one_validated_block(harness_root, tmp_path):
    refs = references_of(harness_root)
    built = CAL.build_scratch_root(harness_root, tmp_path / 'scratch', references=refs, withheld=['T003'])
    assert (built['episodes'], built['released'], built['withheld']) == (10, 9, 1)
    scratch = tmp_path / 'scratch'
    ev = EV.build_evidence(scratch, 1, grader=GRADER, exclude=('T003',))
    assert sorted(ev.episodes) == [1, 2, 4, 5, 6, 7, 8, 9, 10] and ev.excluded == [3]
    assert not (scratch / 'evidence/01/003').exists()                        # a withheld reference is never written
    assert (scratch / 'runs/episodes/001/api').is_symlink() and (scratch / 'runs/episodes/001/task.json').is_file()
    assert all(e.commands for e in ev.episodes.values())                    # the broker records are read through the links
    with pytest.raises(ValueError, match='not empty'):
        CAL.build_scratch_root(harness_root, tmp_path / 'scratch', references=refs)


def test_scratch_root_serves_the_study_tables_index_in_place_of_the_sources(tmp_path):
    """Integration finding: a finished v9 root's own tables index is v9.1 (no narrative section tables), so the
    calibration would never exercise W0-12. With `tables_index`, prepared/ is a real directory whose entries link
    to the source's, except agent/tables/tables.sqlite, which links to the given (version-checked) index."""
    import shutil
    import sqlite3
    from packs.officeqa.tools import otables
    source = tmp_path / 'source' / 'prepared'
    (source / 'agent' / 'corpus').mkdir(parents=True)
    (source / 'agent' / 'corpus' / 'treasury_bulletin_2000_01.txt').write_text(
        'Table T-1. - Synthetic\n\n| Fiscal year | Amount |\n|---|---|\n| 1999 | 1 |\n')
    (source / 'agent' / 'corpus.sqlite').write_bytes(b'fts')
    (source / 'private').mkdir()
    (source / 'private' / 'answers.jsonl').write_text('')
    (source / 'manifest.json').write_text('{}')
    own = source / 'agent' / 'tables' / 'tables.sqlite'
    own.parent.mkdir()
    otables.build_index(source / 'agent' / 'corpus', own)
    own.chmod(0o644)
    with sqlite3.connect(own) as connection:                              # the source's index is an older build
        connection.execute("UPDATE meta SET value = ? WHERE key = 'version'", (json.dumps('otables-index-v9.1'),))
    study = tmp_path / 'study.sqlite'
    otables.build_index(source / 'agent' / 'corpus', study)
    # the default: one link to the source's prepared/ (its own index, whatever its version; recorded)
    plain = CAL.link_prepared(source, tmp_path / 'plain' / 'prepared')
    assert (tmp_path / 'plain' / 'prepared').is_symlink() and plain['tables_index_version'] == 'otables-index-v9.1'
    # the overlay: entries linked one by one, the study index in place of agent/tables/tables.sqlite
    target = tmp_path / 'scratch' / 'prepared'
    info = CAL.link_prepared(source, target, study)
    assert info == {'tables_index': str(study.resolve()), 'tables_index_version': otables.INDEX_VERSION}
    assert target.is_dir() and not target.is_symlink() and (target / 'agent').is_dir() and not (target / 'agent').is_symlink()
    for rel in ('manifest.json', 'private', 'agent/corpus', 'agent/corpus.sqlite'):
        assert (target / rel).is_symlink() and (target / rel).resolve() == (source / rel).resolve(), rel
    served = target / 'agent' / 'tables' / 'tables.sqlite'
    assert served.is_symlink() and served.resolve() == study.resolve()
    assert CAL.data.tables_meta(served)['version'] == otables.INDEX_VERSION
    with pytest.raises(ValueError, match="version 'otables-index-v9.1'"):
        CAL.link_prepared(source, tmp_path / 'refused' / 'prepared', own)
    assert CAL.default_tables_index() in (None, CAL.STUDY_TABLES_INDEX)
    shutil.rmtree(tmp_path / 'scratch')


def test_calibration_runs_the_harness_learner_and_records_the_outcome(harness_root, tmp_path):
    refs = references_of(harness_root)
    ev = EV.build_evidence(harness_root, 2, grader=GRADER)
    step = ev.episodes[1].commands[0]['step']
    actor = FakeActor(refs_of(ITEMS), lambda q, notes, adapter: ('printing_hint' in notes and patched(adapter)) or q in (2, 4, 6, 8, 10))
    out = tmp_path / 'calibration.json'
    record = CAL.calibrate(harness_root, tmp_path / 'scratch', model=ScriptedModel(harness_session(step)), actor_runner=actor,
                           tool_runner=runner_factory(tmp_path), pack=PACK, grader=GRADER,
                           corpus=LC.TextDirCorpus(make_corpus(tmp_path / 'corpus')), exclude=(), references=refs, withheld=[],
                           out=out)
    assert record['status'] == 'admitted' and record['admission']['admitted_revision'] == 'printing_hint', record.get('error')
    assert record['proposals']['by_kind'] == {'stage': 1, 'bundle': 1} and record['proposals']['screen_rejections'] == []
    assert record['replays'][0]['status'] == 'ok' and record['replays'][0]['tier'] == 'additive'
    assert record['admission']['revision']['files'] == ['tools/lookup.py'] and record['actor_runs']['recorded'] == 8
    assert record['cost']['learners']['logical_calls'] == 4 and record['source_tools'] == 'w0'
    assert json.loads(out.read_text())['status'] == 'admitted' and record['path'] == str(out)
    assert 'A-' not in out.read_text() and not any(str(v) in out.read_text() for v in REFERENCES.values() if len(str(v)) > 3)
    summary = preflight.calibration_record(out)
    assert summary['status'] == 'admitted' and summary['admission']['admitted_revision'] == 'printing_hint'
    # the dry run: no proposal, no actor run, a record all the same
    dry = CAL.calibrate(harness_root, tmp_path / 'scratch-dry', actor_runner=actor, tool_runner=runner_factory(tmp_path),
                        pack=PACK, grader=GRADER, corpus=LC.TextDirCorpus(make_corpus(tmp_path / 'corpus2')), exclude=(),
                        references=refs, withheld=[], out=tmp_path / 'dry.json')
    assert dry['dry_run'] and dry['status'] == 'retained' and dry['actor_runs']['recorded'] == 0 and dry['proposals']['by_kind'] == {}
    assert preflight.calibration_record(tmp_path / 'nowhere.json') is None
