"""A benchmark's task set in Harbor's format made Hermit data by `python -m hermit.harbor.dataset prepare` (its
benchmark file's `dataset:` block), without Harbor or a registry: the download (a stand-in), the patch, the rows with
their resources and network modes, the lock and its check on reuse, the splits, the smoke rows and the images'
sizes; and, without Docker, the teardown of a trial's compose projects (hermit.harbor.docker)."""
import errno
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

from hermit.harbor import dataset


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def make_task(root: Path, name: str, *, toml: str = "", image: str | None = None) -> Path:
    task = root / name
    write(task / "task.toml", 'schema_version = "1.4"\n[agent]\ntimeout_sec = 60.0\n[environment]\n'
                              + (f'docker_image = "{image}"\n' if image else "") + toml)
    write(task / "instruction.md", f"Do {name}.\n")
    write(task / "environment" / "setup.py", 'MAPPING = {"seed": 7}\n')
    write(task / "tests" / "test.sh", "#!/bin/bash\necho 1 > /logs/verifier/reward.txt\n")
    return task


class Registry:
    """A stand-in for `harbor datasets download`: the dataset's tasks written where Harbor's export mode puts them
    (`<output>/<name>/<task>/`), counted."""

    def __init__(self, names=("alpha", "beta", "gamma", "delta")):
        self.names, self.calls = names, 0

    def __call__(self, ref: str, output: Path) -> Path:
        self.calls += 1
        tasks = Path(output) / ref.partition("@")[0]
        for i, name in enumerate(self.names):
            make_task(tasks, name, image=f"example/{name}:1" if i % 2 == 0 else None)
        return tasks


PATCH = '''\
from pathlib import Path


def drop_seed(tasks):
    """Each task's setup.py without the seed (idempotent)."""
    changed = 0
    for setup in Path(tasks).glob("*/environment/setup.py"):
        text = setup.read_text()
        if '"seed"' in text:
            setup.write_text(text.replace('{"seed": 7}', '{}'))
            changed += 1
    return f"{changed} setup.py without the seed"
'''


def benchmark(root: Path, **block) -> Path:
    utils = root / "utils"
    write(utils / "patches.py", PATCH)
    spec = {"ref": "demo@1.0", "data": str(root / "data"), "split": {"train": 2, "val": 1, "test": "rest", "seed": 0},
            "smoke": ["beta"], **block}
    return write(utils / "benchmark.yaml", yaml.safe_dump({"tasks_dir": "tasks", "dataset": spec}))


def manifest(size_a: int, size_b: int) -> str:
    """`docker manifest inspect -v` of a two-platform image: the amd64 entry's layers."""
    return json.dumps([
        {"Descriptor": {"platform": {"architecture": "arm64", "os": "linux"}},
         "SchemaV2Manifest": {"layers": [{"size": 999}]}},
        {"Descriptor": {"platform": {"architecture": "amd64", "os": "linux"}},
         "SchemaV2Manifest": {"layers": [{"size": size_a}, {"size": size_b}]}}])


def test_network_modes_are_harbors_resolution():
    assert dataset.network_modes({}) == {"agent": "public", "verifier": "public"}
    legacy = {"environment": {"allow_internet": False}}
    assert dataset.network_modes(legacy) == {"agent": "no-network", "verifier": "no-network"}
    explicit = {"environment": {"allow_internet": False, "network_mode": "public"}}          # the mode wins
    assert dataset.network_modes(explicit)["agent"] == "public"
    phases = {"environment": {"network_mode": "no-network"}, "agent": {"network_mode": "allowlist"},
              "verifier": {"environment": {"network_mode": "public"}},
              "steps": [{"name": "s1", "agent": {"network_mode": "public"}}]}
    assert dataset.network_modes(phases) == {"agent": "allowlist", "verifier": "public"}
    assert dataset.network_modes(phases, "s1") == {"agent": "public", "verifier": "public"}


def test_rows_carry_the_tasks_resources_and_network(tmp_path):
    make_task(tmp_path, "sized", toml='cpus = 2\nmemory = "4G"\nstorage_mb = 10240\nnetwork_mode = "no-network"\n')
    row = dataset.index(tmp_path)[0]
    assert (row["cpus"], row["memory_mb"], row["storage_mb"], row["gpus"]) == (2, 4096, 10240, None)
    assert row["network"] == {"agent": "no-network", "verifier": "no-network"}


def test_prepare_makes_the_examples_data_from_its_benchmark_file(tmp_path):
    registry = Registry()
    file = benchmark(tmp_path, rollouts=2, patch="patches.py:drop_seed", image_sizes=True)
    sizes = {"example/alpha:1": manifest(100, 23), "example/gamma:1": manifest(7, 0)}
    said = []
    written = dataset.prepare(file, download_fn=registry, inspect=lambda image: sizes[image], log=said.append)
    utils = file.parent
    tasks = (utils / "tasks").resolve()
    assert (utils / "tasks").is_symlink() and tasks == (tmp_path / "data" / "demo").resolve() and registry.calls == 1
    assert written["reused"] is False and written["rows"] == 8 and written["smoke"] == 1
    # the patch ran on the download, and the lock records it (its file's digest and what it said)
    assert '"seed"' not in (tasks / "alpha" / "environment" / "setup.py").read_text()
    lock = json.loads((utils / "dataset.lock").read_text())
    assert lock["source"]["ref"] == "demo@1.0" and lock["source"]["patch"]["hook"] == "patches.py:drop_seed"
    assert lock["source"]["patch"]["said"] == "4 setup.py without the seed"
    assert len(lock["source"]["patch"]["sha256"]) == 64
    # rows: k rollouts of a task together, the smoke rows as rollout 0 after them
    ids = [json.loads(line)["id"] for line in (utils / "tasks.jsonl").read_text().splitlines()]
    assert ids[:4] == ["alpha#1", "alpha#2", "beta#1", "beta#2"] and ids[-1] == "beta#0"
    splits = {name: (utils / "splits" / f"{name}.txt").read_text().split() for name in dataset.SPLITS}
    assert len(splits["all"]) == 8 and splits["smoke"] == ["beta#0"]
    assert len(splits["train"]) == 4 and len(splits["val"]) == 2 and len(splits["test"]) == 2   # 2 / 1 / rest tasks
    assert set(splits["train"]) | set(splits["val"]) | set(splits["test"]) == set(splits["all"])
    images = json.loads((utils / "image_sizes.json").read_text())
    assert {k: v for k, v in images.items() if not k.startswith("_")} == {"example/alpha:1": 123, "example/gamma:1": 7}
    # A second prepare reuses the tasks, which still match the lock (the patch, run again, changes nothing): the lock
    # stays as it was, with what the patch said when the tasks were locked
    again = dataset.prepare(file, download_fn=registry, inspect=lambda image: sizes[image], log=said.append)
    assert again["reused"] is True and registry.calls == 1
    assert json.loads((utils / "dataset.lock").read_text())["source"]["patch"]["said"] == "4 setup.py without the seed"
    write(utils / "patches.py", PATCH + "\n# changed\n")             # another patch: not the tasks the lock knows
    with pytest.raises(ValueError, match="patch is not the benchmark's"):
        dataset.prepare(file, download_fn=registry, inspect=lambda image: sizes[image], log=said.append)


def test_a_reused_task_set_that_differs_from_its_lock_is_refused(tmp_path):
    registry = Registry()
    file = benchmark(tmp_path)
    dataset.prepare(file, download_fn=registry, log=lambda m: None)
    tasks = (file.parent / "tasks").resolve()
    write(tasks / "alpha" / "tests" / "test.sh", "#!/bin/bash\necho 0 > /logs/verifier/reward.txt\n")
    with pytest.raises(ValueError, match="changed since the lock: alpha"):
        dataset.prepare(file, download_fn=registry, log=lambda m: None)
    dataset.prepare(file, download_fn=registry, force=True, log=lambda m: None)           # locked anew
    assert dataset.check_lock(tasks, file.parent / "dataset.lock") == []
    other = benchmark(tmp_path, ref="demo@2.0")                       # another version over the same data
    with pytest.raises(ValueError, match="the lock is of demo@1.0"):
        dataset.prepare(other, download_fn=registry, log=lambda m: None)
    assert dataset.main(["prepare", str(other)]) == 1                  # the command says so and fails


def test_an_interrupted_download_is_downloaded_again_not_taken_for_the_task_set(tmp_path):
    """Harbor copies a dataset's tasks into the target a group at a time: a download that stopped part of the way is
    left in a folder of its own, never where prepare looks for the tasks: in the data directory when it is there
    (.demo.partial, which no listing of its tasks takes for one), else beside it (<data>.partial). The next prepare
    removes it, downloads the whole set again and locks all of it, and nothing the stopped download held that the
    set lacks."""
    for case, there in (("new", False), ("existing", True)):
        file = benchmark(tmp_path / case, split="in_sample", smoke=[])
        data = tmp_path / case / "data"
        if there:
            data.mkdir(parents=True)
        partial = data / ".demo.partial" if there else data.with_name("data.partial")

        class Interrupted(Registry):
            def __call__(self, ref, output):
                self.calls, self.output = self.calls + 1, Path(output)
                for name in self.names[:2]:                   # 2 of the 4 tasks copied, then the connection drops
                    make_task(Path(output) / "demo", name)
                make_task(Path(output) / "demo", "stale")     # a task the set lacks, never to be locked with it
                raise RuntimeError("harbor datasets download demo@1.0 failed (1): connection reset")
        interrupted = Interrupted()
        with pytest.raises(RuntimeError, match="connection reset"):
            dataset.prepare(file, download_fn=interrupted, log=lambda m: None)
        assert interrupted.output == partial and (partial / "demo" / "alpha" / "task.toml").is_file(), case
        if there:
            assert dataset.index(data) == [] and dataset.lock_of(data)["tasks"] == {}, case
        registry = Registry()
        written = dataset.prepare(file, download_fn=registry, log=lambda m: None)
        assert registry.calls == 1 and written["reused"] is False and written["rows"] == 4, case
        assert sorted(json.loads((file.parent / "dataset.lock").read_text())["tasks"]) == ["alpha", "beta", "delta",
                                                                                             "gamma"], case
        assert (file.parent / "tasks").resolve() == (data / "demo").resolve(), case
        assert [p.name for p in data.iterdir()] == ["demo"] and not os.path.lexists(partial), case


def test_a_directory_in_the_way_of_the_download_is_named_to_be_moved_away_and_left_as_it_is(tmp_path):
    """A directory where the download goes (<data>/<name>) that holds none of the set's tasks cannot be replaced by
    it: prepare names it and says to move it away, on this prepare and the next, leaving it as it was; once it is
    moved away, the download goes there."""
    file = benchmark(tmp_path, split="in_sample", smoke=[])
    write(tmp_path / "data" / "demo" / "alpha" / "notes.txt", "not a task\n")
    for _ in range(2):
        with pytest.raises(ValueError, match=r"data/demo holds none of demo@1.0's tasks .*: move it away"):
            dataset.prepare(file, download_fn=Registry(), log=lambda m: None)
    assert [p.name for p in (tmp_path / "data").iterdir()] == ["demo"]
    assert (tmp_path / "data" / "demo" / "alpha" / "notes.txt").read_text() == "not a task\n"
    (tmp_path / "data" / "demo").rename(tmp_path / "moved")
    written = dataset.prepare(file, download_fn=Registry(), log=lambda m: None)
    assert written["rows"] == 4 and (file.parent / "tasks").resolve() == (tmp_path / "data" / "demo").resolve()


def test_what_is_in_the_way_at_the_sets_own_name_is_refused_before_anything_is_downloaded(tmp_path):
    """Harbor's export writes only <output>/<name>/, where prepare looks for the tasks: what is in the way there (a
    directory holding none of the set's tasks, a file, a link) is refused before the download, so a prepare refused
    for it, again and again, downloads nothing and leaves no partial download in the data directory or beside it."""
    registry = Registry()
    for case, make in (("directory", lambda there: write(there / "alpha" / "notes.txt", "not a task\n")),
                       ("file", lambda there: write(there, "not a task\n")),
                       ("link", lambda there: there.symlink_to(there.parent.parent / "elsewhere"))):
        file = benchmark(tmp_path / case, split="in_sample", smoke=[])
        there = tmp_path / case / "data" / "demo"
        there.parent.mkdir(parents=True, exist_ok=True)
        make(there)
        for _ in range(2):
            with pytest.raises(ValueError, match=r"data/demo holds none of demo@1.0's tasks .*: move it away"):
                dataset.prepare(file, download_fn=registry, log=lambda m: None)
        assert registry.calls == 0 and not os.path.lexists(tmp_path / case / "data.partial"), case
        assert [p.name for p in there.parent.iterdir()] == ["demo"], case


def test_another_entry_of_the_download_in_the_way_is_refused_once_it_returned_and_nothing_is_moved(tmp_path):
    """An entry of the download beside the set's own directory, where something is in the way in the data directory,
    is refused once the download returned: nothing of it is moved there, and what was there is left as it was; the
    download stays in its own folder, which the next prepare, once that is moved away, removes and downloads again."""
    class WithNotes(Registry):
        def __call__(self, ref, output):
            write(Path(output) / "notes.txt", "the download's\n")
            return super().__call__(ref, output)
    file = benchmark(tmp_path, split="in_sample", smoke=[])
    write(tmp_path / "data" / "notes.txt", "mine\n")
    registry = WithNotes()
    with pytest.raises(ValueError, match=r"data/notes.txt holds none of demo@1.0's tasks .*: move it away"):
        dataset.prepare(file, download_fn=registry, log=lambda m: None)
    assert registry.calls == 1                                       # refused once the download returned
    assert sorted(p.name for p in (tmp_path / "data").iterdir()) == [".demo.partial", "notes.txt"]
    assert (tmp_path / "data" / "notes.txt").read_text() == "mine\n"
    (tmp_path / "data" / "notes.txt").rename(tmp_path / "moved.txt")
    written = dataset.prepare(file, download_fn=registry, log=lambda m: None)
    assert registry.calls == 2 and written["rows"] == 4
    assert sorted(p.name for p in (tmp_path / "data").iterdir()) == ["demo", "notes.txt"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes in any folder")
def test_a_data_directory_in_a_folder_the_user_cannot_write_is_downloaded_into_from_a_folder_in_it(tmp_path):
    """A data directory of the user's own in a folder they cannot write (one an admin made, /data/<user>; a mount
    point under a root-owned /) is prepared: the download is made in it, nothing beside it."""
    file = benchmark(tmp_path, split="in_sample", smoke=[])
    shared = tmp_path / "shared"
    (shared / "me").mkdir(parents=True)
    shared.chmod(0o555)
    try:
        written = dataset.prepare(file, data=str(shared / "me"), download_fn=Registry(), log=lambda m: None)
    finally:
        shared.chmod(0o755)
    assert written["rows"] == 4 and (file.parent / "tasks").resolve() == (shared / "me" / "demo").resolve()
    assert [p.name for p in shared.iterdir()] == ["me"] and [p.name for p in (shared / "me").iterdir()] == ["demo"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes in any folder")
def test_a_data_directory_this_user_cannot_write_in_or_another_users_lock_is_the_commands_error_not_a_traceback(
        tmp_path, monkeypatch, capsys):
    """A download that cannot make or open its lock (in a data directory this user cannot write in, or the
    .<name>.partial.lock another user's prepare left, which this user cannot open to write) is refused before anything
    is downloaded: prepare raises the ValueError the command says as its error (`error: ...` naming the path, exit 1),
    not an OSError's traceback, and leaves the data directory as it was."""
    calls = []
    monkeypatch.setattr(dataset, "download", lambda *args, **kwargs: calls.append(args))   # no Harbor, no network
    registry = Registry()
    for case in ("unwritable", "lock"):
        file = benchmark(tmp_path / case, split="in_sample", smoke=[])
        data = tmp_path / case / "data"
        data.mkdir(parents=True)
        data = data.resolve()                            # as prepare names it
        lock = data / ".demo.partial.lock"
        if case == "lock":
            write(lock, "").chmod(0o444)                 # another user's: this one may read it, not write it
        else:
            data.chmod(0o555)
        try:
            with pytest.raises(ValueError, match=r"demo@1.0 could not be downloaded into .*/data: .*Permission denied"):
                dataset.prepare(file, download_fn=registry, log=lambda m: None)
            assert dataset.main(["prepare", str(file)]) == 1, case
        finally:
            data.chmod(0o755)
        err = capsys.readouterr().err
        assert err.startswith(f"error: demo@1.0 could not be downloaded into {data}: ") and str(lock) in err, case
        assert registry.calls == 0 and calls == [], case
        assert [p.name for p in data.iterdir()] == ([lock.name] if case == "lock" else []), case


def test_a_data_directory_that_is_a_mount_point_is_downloaded_into_on_its_own_filesystem(tmp_path, monkeypatch):
    """A data directory that is a mount point is a filesystem of its own, which no entry can be renamed into from
    beside it (EXDEV): the download is made in it, so its entries are moved within it and the set is prepared on the
    first prepare, not downloaded again on every one."""
    file = benchmark(tmp_path, split="in_sample", smoke=[])
    data = (tmp_path / "data").resolve()
    data.mkdir()
    rename = os.replace

    def replace(src, dst):                                  # data's own filesystem: no rename across its boundary
        if Path(src).resolve().is_relative_to(data) != Path(dst).resolve().is_relative_to(data):
            raise OSError(errno.EXDEV, "Invalid cross-device link", str(src))
        rename(src, dst)
    monkeypatch.setattr(dataset.os, "replace", replace)
    registry = Registry()
    written = dataset.prepare(file, download_fn=registry, log=lambda m: None)
    assert registry.calls == 1 and written["rows"] == 4
    assert (file.parent / "tasks").resolve() == data / "demo" and [p.name for p in data.iterdir()] == ["demo"]


def test_a_second_prepare_of_the_set_at_the_same_time_is_refused_and_leaves_the_first_ones_download(tmp_path):
    """Two prepares of one set into one data directory at once (there or not yet): the second is refused while the
    first downloads, never removing what the first has downloaded so far, and the first prepares the whole set."""
    for case, there in (("new", False), ("existing", True)):
        file = benchmark(tmp_path / case, split="in_sample", smoke=[])
        if there:
            (tmp_path / case / "data").mkdir(parents=True)
        second = Registry()

        class First(Registry):
            def __call__(self, ref, output):
                self.calls += 1
                for name in self.names[:2]:
                    make_task(Path(output) / "demo", name)
                with pytest.raises(ValueError, match="another prepare is downloading demo@1.0 into .*: wait for it"):
                    dataset.prepare(file, download_fn=second, log=lambda m: None)
                for name in self.names[2:]:
                    make_task(Path(output) / "demo", name)
                return Path(output) / "demo"
        written = dataset.prepare(file, download_fn=First(), log=lambda m: None)
        assert second.calls == 0 and written["rows"] == 4, case
        assert sorted(json.loads((file.parent / "dataset.lock").read_text())["tasks"]) == ["alpha", "beta", "delta",
                                                                                             "gamma"], case
        assert [p.name for p in (tmp_path / case / "data").iterdir()] == ["demo"], case
        assert sorted(p.name for p in (tmp_path / case).iterdir()) == ["data", "utils"], case


def test_a_data_directory_on_a_filesystem_that_keeps_no_file_locks_is_prepared_unguarded(tmp_path, monkeypatch):
    """A filesystem that keeps no file locks (NFS without its lock daemon: ENOLCK) does not stop a prepare: the set is
    downloaded and prepared as before prepares were held one at a time, and no lock file is left beside the set."""
    def no_locks(fd, operation):
        raise OSError(errno.ENOLCK, "No locks available")
    monkeypatch.setattr(dataset.fcntl, "flock", no_locks)
    for case, there in (("new", False), ("existing", True)):
        file = benchmark(tmp_path / case, split="in_sample", smoke=[])
        if there:
            (tmp_path / case / "data").mkdir(parents=True)
        written = dataset.prepare(file, download_fn=Registry(), log=lambda m: None)
        assert written["rows"] == 4 and [p.name for p in (tmp_path / case / "data").iterdir()] == ["demo"], case
        assert sorted(p.name for p in (tmp_path / case).iterdir()) == ["data", "utils"], case


def test_a_directory_of_its_own_where_the_tasks_link_goes_is_refused_before_anything_is_downloaded(tmp_path):
    """What is at the place of the `tasks` link beside the benchmark file and is no link (a directory of the user's)
    is never replaced by the link: prepare names it, to be moved away, before anything is downloaded, again and
    again, and leaves it as it was."""
    registry = Registry()
    file = benchmark(tmp_path, split="in_sample", smoke=[])
    write(file.parent / "tasks" / "mine.txt", "mine\n")
    for _ in range(2):
        with pytest.raises(ValueError, match=r"utils/tasks is a directory of its own, not the link prepare writes"):
            dataset.prepare(file, download_fn=registry, log=lambda m: None)
    assert registry.calls == 0 and sorted(p.name for p in tmp_path.iterdir()) == ["utils"]
    assert (file.parent / "tasks" / "mine.txt").read_text() == "mine\n"


def test_a_file_where_the_tasks_link_goes_is_named_a_file_and_refused_before_anything_is_downloaded(tmp_path):
    """What is at the place of the `tasks` link and is no link is named as what it is: a file of the user's there is
    a file, not a directory, refused, to be moved away, before anything is downloaded, again and again, and left as it
    was."""
    registry = Registry()
    file = benchmark(tmp_path, split="in_sample", smoke=[])
    write(file.parent / "tasks", "mine\n")
    for _ in range(2):
        with pytest.raises(ValueError, match=r"utils/tasks is a file of its own, not the link prepare writes: move"):
            dataset.prepare(file, download_fn=registry, log=lambda m: None)
    assert registry.calls == 0 and sorted(p.name for p in tmp_path.iterdir()) == ["utils"]
    assert (file.parent / "tasks").read_text() == "mine\n"


def test_a_data_directory_that_is_a_file_is_refused_before_anything_is_downloaded(tmp_path):
    """The data directory itself a file (or a link to one, which the data path is read through) has no room for the
    set: it is refused as what is in the way at <data>/<name> is, before the download, so a prepare refused for it,
    again and again, downloads nothing, leaves no <data>.partial and leaves the file as it was."""
    registry = Registry()
    for case, make in (("file", lambda there: write(there, "mine\n")),
                       ("link", lambda there: there.symlink_to(write(there.parent / "elsewhere", "mine\n")))):
        file = benchmark(tmp_path / case, split="in_sample", smoke=[])
        there = tmp_path / case / "data"
        make(there)
        for _ in range(2):
            with pytest.raises(ValueError, match=r"/(data|elsewhere) holds none of demo@1.0's tasks .*: move it away"):
                dataset.prepare(file, download_fn=registry, log=lambda m: None)
        assert registry.calls == 0 and not list((tmp_path / case).glob("*.partial")), case
        assert there.read_text() == "mine\n", case


def test_a_data_directory_at_the_tasks_link_or_in_it_is_refused_before_anything_is_downloaded(tmp_path):
    """The `tasks` link prepare writes beside the benchmark file is no place for the data directory: one there or in
    it (`data: tasks`, as the configs' tasks_dir is spelled, or --data <its folder>/tasks) is refused before anything
    is downloaded or reused, again and again, leaving nothing beside the file. It is read as it is spelled: a link an
    earlier prepare left there is not followed, so the benchmark file is refused alike where that link is."""
    registry = Registry()
    refused = r"is the link prepare writes to the tasks \(.*/utils/tasks\) or in it: name another"
    for case, block, flag in (("data", {"data": "tasks"}, None), ("under", {"data": "tasks/sets"}, None),
                              ("flag", {}, "tasks")):
        file = benchmark(tmp_path / case, split="in_sample", smoke=[], **block)
        for _ in range(2):
            with pytest.raises(ValueError, match=refused):
                dataset.prepare(file, download_fn=registry, data=flag and str(file.parent / flag), log=lambda m: None)
        assert registry.calls == 0, case
        assert sorted(p.name for p in file.parent.iterdir()) == ["benchmark.yaml", "patches.py"], case
    file = benchmark(tmp_path / "linked", split="in_sample", smoke=[])
    dataset.prepare(file, download_fn=registry, log=lambda m: None)              # the link to <root>/data/demo
    benchmark(tmp_path / "linked", split="in_sample", smoke=[], data="tasks")
    with pytest.raises(ValueError, match=refused):
        dataset.prepare(file, download_fn=registry, log=lambda m: None)
    assert dataset.main(["prepare", str(file)]) == 1                             # the command says so and fails
    assert registry.calls == 1
    assert (file.parent / "tasks").resolve() == (tmp_path / "linked" / "data" / "demo").resolve()


def test_a_tasks_digest_is_the_same_under_another_umask(tmp_path):
    """The lock is shared (committed beside the benchmark file, checked on another account): of each file's mode a
    task's digest keeps only whether it is executable, as git does, not the bits a umask sets for the group and
    others."""
    digests = []
    for umask in (0o022, 0o002, 0o077):
        old = os.umask(umask)
        try:
            task = make_task(tmp_path / f"u{umask:03o}", "alpha")
            (task / "tests" / "test.sh").chmod(0o777 & ~umask)              # executable, as a download leaves it
        finally:
            os.umask(old)
        digests.append(dataset.digest(task))
    assert len(set(digests)) == 1
    (task / "tests" / "test.sh").chmod(0o600)                                # no longer executable: another task
    assert dataset.digest(task) != digests[-1]


def test_the_lock_names_no_path_of_this_machine_and_a_prepare_that_changes_nothing_leaves_it_alone(tmp_path):
    """dataset.lock is committed beside the benchmark file: it names no directory of this machine, and a prepare that
    finds the tasks and their source as locked (the patch, run again on the reuse, now says it changed nothing) leaves
    the file as it was; tasks locked anew (--force) rewrite it."""
    file = benchmark(tmp_path, patch="patches.py:drop_seed")
    registry = Registry()
    dataset.prepare(file, download_fn=registry, log=lambda m: None)
    lock_path = file.parent / "dataset.lock"
    locked = lock_path.read_text()
    assert str(tmp_path) not in locked and "tasks_dir" not in json.loads(locked)
    dataset.prepare(file, download_fn=registry, log=lambda m: None)
    assert lock_path.read_text() == locked
    tasks = (file.parent / "tasks").resolve()
    write(tasks / "alpha" / "tests" / "test.sh", "#!/bin/bash\necho 0 > /logs/verifier/reward.txt\n")
    dataset.prepare(file, download_fn=registry, force=True, log=lambda m: None)
    assert lock_path.read_text() != locked and dataset.check_lock(tasks, lock_path) == []


def test_a_lock_that_is_json_but_no_lock_is_unreadable_and_refused_unless_forced(tmp_path):
    """A dataset.lock that parses but is no lock (no object, or its tasks or its source no object) is unreadable, as
    one that does not parse: prepare refuses it with a ValueError (the command's error, not a traceback) unless
    --force, which locks the tasks anew, and `dataset check` says it is unreadable."""
    registry = Registry()
    file = benchmark(tmp_path, split="in_sample", smoke=[])
    dataset.prepare(file, download_fn=registry, log=lambda m: None)
    lock_path, tasks = file.parent / "dataset.lock", (file.parent / "tasks").resolve()
    good = json.loads(lock_path.read_text())
    for bad in ([], "demo@1.0", {**good, "tasks": ["alpha"]}, {**good, "source": "demo@1.0"}):
        lock_path.write_text(json.dumps(bad))
        with pytest.raises(ValueError, match="lock unreadable: (not an object with the tasks' digests|its source)"):
            dataset.prepare(file, download_fn=registry, log=lambda m: None)
        assert dataset.main(["prepare", str(file)]) == 1, bad
        dataset.prepare(file, download_fn=registry, force=True, log=lambda m: None)
        assert json.loads(lock_path.read_text())["tasks"] == good["tasks"], bad
    for bad in ([], {**good, "tasks": ["alpha"]}):
        lock_path.write_text(json.dumps(bad))
        assert dataset.check_lock(tasks, lock_path) == ["lock unreadable: not an object with the tasks' digests"]
        assert dataset.main(["check", str(tasks), "--lock", str(lock_path)]) == 1
    assert registry.calls == 1


def test_a_lock_nested_past_the_parsers_depth_or_that_cannot_be_read_is_unreadable_and_refused_unless_forced(
        tmp_path, monkeypatch, capsys):
    """A dataset.lock the JSON parser gives up on (nested past its depth) or whose file cannot be read is unreadable,
    as one that is no JSON: prepare refuses it with a ValueError (the command's error, not a traceback) unless
    --force, which locks the tasks anew, and `dataset check` says it is unreadable."""
    registry = Registry()
    file = benchmark(tmp_path, split="in_sample", smoke=[])
    dataset.prepare(file, download_fn=registry, log=lambda m: None)
    lock_path, tasks = file.parent / "dataset.lock", (file.parent / "tasks").resolve()
    good = json.loads(lock_path.read_text())
    read = Path.read_text

    def unreadable(path, *args, **kwargs):
        if path == lock_path:
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return read(path, *args, **kwargs)
    for case, why in (("deep", "maximum recursion depth exceeded"), ("denied", "Permission denied")):
        if case == "deep":
            lock_path.write_text("[" * 100_000 + "]" * 100_000)
        else:
            monkeypatch.setattr(Path, "read_text", unreadable)
        with pytest.raises(ValueError, match=f"--force locks them anew\\): lock unreadable: .*{why}"):
            dataset.prepare(file, download_fn=registry, log=lambda m: None)
        assert dataset.main(["prepare", str(file)]) == 1, case
        assert capsys.readouterr().err.startswith("error: the tasks at "), case
        assert dataset.check_lock(tasks, lock_path)[0].startswith("lock unreadable: "), case
        assert dataset.main(["check", str(tasks), "--lock", str(lock_path)]) == 1, case
        dataset.prepare(file, download_fn=registry, force=True, log=lambda m: None)
        monkeypatch.undo()
        assert json.loads(lock_path.read_text())["tasks"] == good["tasks"], case
    assert registry.calls == 1


def test_a_relative_data_directory_is_the_benchmark_files_not_the_working_directorys(tmp_path, monkeypatch):
    """The block's `data:` is relative to the benchmark file, as its other paths are (the patch; the runner's
    tasks_dir); a data directory given on the command line (--data) is the shell's."""
    file = benchmark(tmp_path, data="../data", split="in_sample", smoke=[])
    elsewhere = tmp_path / "a" / "b"
    elsewhere.mkdir(parents=True)
    monkeypatch.chdir(elsewhere)
    dataset.prepare(file, download_fn=Registry(), log=lambda m: None)
    assert (file.parent / "tasks").resolve() == (tmp_path / "data" / "demo").resolve()
    assert not (tmp_path / "a" / "data").exists()
    assert dataset.data_dir("demo@1.0", "x") == (elsewhere / "x").resolve()


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_git_ignores_the_tasks_link_prepare_writes_beside_an_examples_benchmark_file(tmp_path):
    """The `tasks` link names a directory of this machine: the repository's .gitignore keeps it out of an example's
    commits (a link, which a pattern ending in / would not match), where conftest.py keeps it out of pytest's
    collection."""
    repo, data = tmp_path / "repo", tmp_path / "data"
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    shutil.copy(Path(__file__).resolve().parents[2] / ".gitignore", repo / ".gitignore")
    utils = repo / "examples" / "some-benchmark" / "utils"
    utils.mkdir(parents=True)
    data.mkdir()
    (utils / "tasks").symlink_to(data, target_is_directory=True)
    run = subprocess.run(["git", "-C", str(repo), "check-ignore", "-v", "examples/some-benchmark/utils/tasks"],
                         capture_output=True, text=True)
    assert run.returncode == 0 and run.stdout.startswith(".gitignore:"), run


def test_a_trials_compose_projects_are_taken_down_from_an_empty_directory_of_their_own(tmp_path, monkeypatch):
    """hermit.harbor.docker.compose_down names the trial's projects alone (`-p`): it runs where no compose file or
    .env of this process's working directory can be read, whose volumes `down --volumes` would remove too."""
    from hermit.harbor import docker
    (tmp_path / "compose.yaml").write_text("services: {}\nvolumes: {mine: {}}\n")
    (tmp_path / ".env").write_text("COMPOSE_FILE=compose.yaml\n")
    monkeypatch.chdir(tmp_path)
    downs = []

    class Done:
        stdout = "alpha__abc1234__verifier__main\n"

    def run(cmd, **kwargs):
        if "down" in cmd:
            where = Path(kwargs.get("cwd") or Path.cwd())
            downs.append((cmd[cmd.index("-p") + 1], where, sorted(p.name for p in where.iterdir())))
        return Done()
    monkeypatch.setattr(docker.subprocess, "run", run)
    docker.compose_down("alpha__abc1234")
    assert [project for project, _, _ in downs] == ["alpha__abc1234__env", "alpha__abc1234__verifier__main"]
    assert all(where != tmp_path and entries == [] for _, where, entries in downs), downs


def test_a_trials_compose_projects_are_taken_down_with_none_of_this_processs_compose_variables(tmp_path, monkeypatch):
    """A compose file this process's environment selects (COMPOSE_FILE, and its profiles, env files, project
    directory) is not read by the teardown either, whose `down --volumes` would remove its volumes too: each `down`
    runs with none of the COMPOSE_* variables, and with every other one (the Docker daemon it reaches)."""
    from hermit.harbor import docker
    (tmp_path / "compose.yaml").write_text("services: {}\nvolumes: {mine: {name: mine}}\n")
    for name, value in (("COMPOSE_FILE", str(tmp_path / "compose.yaml")), ("COMPOSE_PROFILES", "all"),
                        ("COMPOSE_ENV_FILES", str(tmp_path / ".env")), ("COMPOSE_PROJECT_DIRECTORY", str(tmp_path)),
                        ("COMPOSE_PATH_SEPARATOR", ":"), ("DOCKER_HOST", "unix:///run/another-docker.sock")):
        monkeypatch.setenv(name, value)
    envs = []

    class Done:
        stdout = "alpha__abc1234__verifier__main\n"

    def run(cmd, **kwargs):
        if "down" in cmd:
            envs.append(dict(os.environ if kwargs.get("env") is None else kwargs["env"]))   # None: this process's
        return Done()
    monkeypatch.setattr(docker.subprocess, "run", run)
    docker.compose_down("alpha__abc1234")
    assert len(envs) == 2
    for env in envs:
        assert [k for k in env if k.startswith("COMPOSE_")] == []
        assert env["DOCKER_HOST"] == "unix:///run/another-docker.sock" and env["PATH"] == os.environ["PATH"]


def test_with_no_room_for_an_empty_directory_a_trials_projects_are_taken_down_from_the_root(tmp_path, monkeypatch):
    """With no room for an empty directory (a full /tmp) the teardown still takes the trial's projects down, from
    `/`, not from this process's working directory and its compose file."""
    from hermit.harbor import docker
    (tmp_path / "compose.yaml").write_text("services: {}\nvolumes: {mine: {}}\n")
    monkeypatch.chdir(tmp_path)

    def full(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(docker.tempfile, "TemporaryDirectory", full)
    downs = []

    class Done:
        stdout = "alpha__abc1234__verifier__main\n"

    def run(cmd, **kwargs):
        if "down" in cmd:
            downs.append((cmd[cmd.index("-p") + 1], kwargs.get("cwd")))
        return Done()
    monkeypatch.setattr(docker.subprocess, "run", run)
    docker.compose_down("alpha__abc1234")
    assert downs == [("alpha__abc1234__env", "/"), ("alpha__abc1234__verifier__main", "/")]


def test_prepare_refuses_what_it_cannot_make(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="name@version"):
        dataset.prepare(benchmark(tmp_path, ref="demo"), download_fn=Registry(), log=lambda m: None)
    with pytest.raises(ValueError, match="name@version"):
        dataset.prepare(benchmark(tmp_path, ref="demo@head"), download_fn=Registry(), log=lambda m: None)
    with pytest.raises(ValueError, match="unknown keys"):
        dataset.prepare(benchmark(tmp_path, shuffle=True), download_fn=Registry(), log=lambda m: None)
    with pytest.raises(ValueError, match="smoke names tasks"):
        dataset.prepare(benchmark(tmp_path, smoke=["omega"]), download_fn=Registry(), log=lambda m: None)
    with pytest.raises(ValueError, match="asked for"):
        dataset.prepare(benchmark(tmp_path, split={"train": 5, "test": 1}), download_fn=Registry(), log=lambda m: None)
    in_sample = benchmark(tmp_path / "s", split="in_sample", smoke=[])
    dataset.prepare(in_sample, download_fn=Registry(), log=lambda m: None)
    splits = in_sample.parent / "splits"
    assert (splits / "train.txt").read_text() == (splits / "test.txt").read_text() == (splits / "all.txt").read_text()
    assert (splits / "val.txt").read_text() == "" and (splits / "smoke.txt").read_text() == ""
    # the data's default home: the root every prepared task set shares, then the ref
    monkeypatch.setenv(dataset.TASKS_ROOT_ENV, str(tmp_path / "root"))
    assert dataset.data_dir("demo@1.0") == (tmp_path / "root" / "demo@1.0").resolve()
    assert dataset.data_dir("org/demo@1.0", "~/x") == Path("~/x").expanduser().resolve()


def test_an_image_manifest_gives_its_compressed_size():
    assert dataset.manifest_size(manifest(10, 5)) == 15
    single = json.dumps({"SchemaV2Manifest": {"layers": [{"size": 4}, {"size": 6}]}})
    assert dataset.manifest_size(single) == 10
    assert dataset.manifest_size("not json") is None and dataset.manifest_size("[]") is None
