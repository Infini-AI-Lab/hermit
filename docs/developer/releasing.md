# Release Hermit

Use one source revision for the Git tag, GitHub release and PyPI distributions. A GitHub release alone does not
update the package installed by pip.

## 1. Prepare the release

- Pull the latest main and choose the version and included changes. Preserve unrelated local work.
- Update `pyproject.toml`, `hermit/__init__.py`, `CITATION.cff`, the README version badge and docs version labels.
  Keep historical release references and upstream package versions unchanged.
- Move the accumulated changelog under the new version and planned release date, leaving an Unreleased section.
  Explain breaking changes and link migration instructions.
- Draft the GitHub notes: Highlights by feature, Breaking Changes, What's Changed, New Contributors and Full
  Changelog. Check contributor attribution against the project's release policy and review the generated list.

## 2. Validate the source and distributions

Run the [contribution checks](https://github.com/Infini-AI-Lab/hermit/blob/main/CONTRIBUTING.md), affected method tests,
the offline Quick Start and the strict documentation build. Record failures, skips and platform limits. Linux
sandbox checks need a Linux environment; a successful macOS build does not verify them.

Run the `Release checks and PyPI` workflow manually on the candidate revision for Linux core/sandbox checks,
distribution builds and an installed-wheel smoke. Manual dispatch validates without publishing.

In a clean source checkout and a packaging environment with `build` and `twine` installed:

```bash
python -m build
python -m twine check dist/*
```

Use a fresh `dist/` directory for each release. Check that the wheel and source distribution contain all bundled
templates and methods, including hidden metadata and resource files, and no credentials, run artifacts or caches.

Install the wheel into a separate, clean environment. Check its installed version and `hermit --help`, then run the
offline Quick Start through check, train, evaluation and deployment. Test from outside the source checkout so it
cannot hide missing packaged files. Optional method dependencies still require their documented installation.

## 3. Publish the tested revision

Commit the release preparation and validate the final revision. Create and push its `vX.Y.Z` tag. The
`.github/workflows/release.yml` workflow verifies that the tag matches the package version, reruns the Linux checks,
then publishes the built wheel and source distribution to the existing
[`hermit-harness` PyPI project](https://pypi.org/project/hermit-harness/).

One-time setup: a PyPI project maintainer adds a GitHub Trusted Publisher with owner `Infini-AI-Lab`, repository
`hermit`, workflow `release.yml` and environment `pypi`. The workflow uses short-lived credentials rather than a
stored PyPI token. Wait for publication to succeed, then publish the reviewed GitHub release notes for the same tag.

For a manual publication instead, upload the matching, tested distributions with Twine:

```bash
python -m twine upload dist/*
```

Authenticate through the publishing tool; keep credentials out of source files, command arguments and shared logs.
See [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/) for the automated setup. Creating a GitHub
release by itself does not trigger this tag-based PyPI workflow.

Published versions are immutable. Correct a bad release with a new version rather than replacing its files or
moving its tag.

## 4. Verify the published release

In another fresh environment, install the exact version from PyPI, check the installed version and command, and run
the offline smoke again. Confirm that the GitHub tag, release notes, package metadata and documentation agree.
Update the README News announcement and share the release once public installation works.

The maintainer confirms the release scope, notes and publication. An agent can prepare the changes, run the checks,
build the distributions and report any remaining limits; commit, tag, push and publication follow the maintainer's
instructions.
