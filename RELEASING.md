# Releasing

PyPI projects and uploaded files are public. Before the first upload, confirm
that NVTA has authorized public redistribution of every calibrated dictionary,
lookup table, and audit file included under `src/nvta_taplite_workflow`.

1. Update the version in `pyproject.toml` and `CHANGELOG.md`.
2. Run the full tests on Python 3.11 Windows.
3. Build with `python -m build` and validate with `python -m twine check dist/*`.
4. Install the wheel into a clean environment and run
   `python -m nvta_taplite_workflow doctor`.
5. Tag the commit as `v<version>` and push the tag.
6. The `release.yml` GitHub workflow tests the wheel and publishes it through
   PyPI Trusted Publishing.

Before the first release, configure the GitHub repository's `pypi` environment
and register `.github/workflows/release.yml` as a Trusted Publisher for the
`nvta-taplite-workflow` PyPI project. Require a maintainer approval on the
GitHub `pypi` environment.

PyPI releases are immutable. Never reuse a version number.
