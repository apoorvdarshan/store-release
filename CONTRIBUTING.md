# Contributing

store-release is maintained by one person and used by several apps' release
pipelines, so changes land carefully.

- **Pull requests only.** `main` changes through pull requests; each must pass CI
  (`test (stdlib)`, `test (strict)`, `test (macos)`, `lint`, `workflows`, CodeQL and
  dependency review) before it is merged.
- **Tests with every change.** Run the suites before opening a PR:

  ```bash
  python3 -I -u run.py selftest --platform all            # standard library only
  python3 -m pip install --require-hashes -r requirements.txt -r requirements-checks.txt
  python3 -I -u run.py selftest --platform all --suite unit --suite apps --suite parity \
    --suite workflows --require-pillow --strict
  python3 -m pip install --require-hashes -r requirements-lint.txt && python3 -m ruff check .
  ```

  The workflow job also runs `actionlint` 1.7.12 on `.github/workflows/`.
- **A CHANGELOG entry per PR** under `## [Unreleased]`, with the change's Impact
  (`checks-only`, `validation`, `release-path` or `config-breaking`) and any app
  actions required ([release process](docs/release-process.md)).
- **Never edit `tests/parity/upstream_*`.** Those are verbatim copies of the
  upstream scripts, hashed in `MANIFEST.sha256`; the parity tests compare against
  them.
- **No app-specific values** in `store_release/`, `.github/workflows/` or
  `.github/actions/`; they belong in an app's `store/store-release.toml`. A test
  enforces this.
- **Keep behaviour.** A change that alters what reaches a store needs its own config
  key with a default that keeps today's behaviour, unless it fixes a bug.
- **No credentials** in the repository, ever: no keys, certificates, keystores,
  service-account JSON or tokens, not even expired ones. Tests generate what they
  need at run time.
- Pin third-party actions by full commit SHA with a `# vX.Y.Z` comment, and Python
  dependencies by hash (`pip-compile --generate-hashes`, pip-tools 7.6.2, Python 3.13).

Security issues: see [SECURITY.md](SECURITY.md), not public issues.
