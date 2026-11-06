"""AHE's rollback helpers on the adapter's repository (hermit/agent/evolver/methods/ahe_adapter/rollback.py)."""


def test_the_best_commit_is_exported_without_the_repositorys_filters(tmp_path):
    """The adapter's .git is the learner's to write: a smudge filter it configures does not run when the best
    commit is exported (git archive would run it); files keep their bytes and executable bit, a link is left out."""
    import os
    import subprocess
    from hermit.agent.evolver.methods.ahe_adapter.rollback import export
    repo, marker, dest = tmp_path / "adapter", tmp_path / "ran", tmp_path / "out"
    repo.mkdir()
    git = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                                    check=True, capture_output=True)
    git("init", "-q")
    (repo / "a.txt").write_text("alpha\n")
    (repo / "run.sh").write_text("#!/bin/sh\n")
    os.chmod(repo / "run.sh", 0o755)
    (repo / ".gitattributes").write_text("* filter=evil\n")
    os.symlink("/etc/passwd", repo / "link")
    os.symlink("a.txt", repo / "alias.txt")                    # inside the adapter: kept
    git("add", "-A")
    git("commit", "-q", "-m", "c")
    git("config", "filter.evil.smudge", f"touch {marker}; cat")
    commit = git("rev-parse", "HEAD").stdout.decode().strip()
    assert export(repo, commit, dest)
    assert not marker.exists()
    assert (dest / "a.txt").read_text() == "alpha\n" and os.access(dest / "run.sh", os.X_OK)
    assert not (dest / "link").exists() and not (dest / "link").is_symlink()
    assert os.readlink(dest / "alias.txt") == "a.txt" and (dest / "alias.txt").read_text() == "alpha\n"
    assert not export(repo, "0" * 40, tmp_path / "none")
