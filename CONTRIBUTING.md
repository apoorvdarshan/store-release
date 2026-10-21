# Contributing

Thanks for helping. store-release ships other people's apps, so changes land carefully:
every change comes with tests, and anything that alters what reaches a store is opt-in.

## Ways to help

- **Report a bug** in an issue: the store-release version, the command or workflow, the
  error, and the redacted log lines around it. Never paste a key, certificate, keystore,
  service-account JSON or token; check logs before you attach them. Security problems go
  privately ([SECURITY.md](SECURITY.md)), not to issues.
- **Ask for a feature** in an issue first, with the store behaviour you need. The
  [roadmap](docs/roadmap.md) lists planned work and known gaps.
- **Send a pull request.** Small, focused pull requests are easiest to review; for a
  larger change, agree on the approach in an issue first.

## Set up

Python 3.13 (the package supports 3.11 and later); the standard library is enough for the
unit suite.

```bash
git clone https://github.com/apoorvdarshan/store-release && cd store-release
python3 -I -u run.py selftest --platform all            # standard library only
python3 -m venv .venv && . .venv/bin/activate
python3 -m pip install --require-hashes -r requirements.txt -r requirements-checks.txt
python3 -I -u run.py selftest --platform all --suite unit --suite apps --suite parity \
  --suite workflows --require-pillow --strict          # what CI's test (strict) runs
python3 -m pip install --require-hashes -r requirements-lint.txt && python3 -m ruff check .
python3 -I -u scripts/render_examples.py --check        # examples and templates are current
```

The PyPI package, as CI's `package` job builds and checks it (its own environment: the
dist lock pins other versions than the store lock):

```bash
python3 -m venv .venv-dist && .venv-dist/bin/python -m pip install --require-hashes -r requirements-dist.txt
.venv-dist/bin/python -I scripts/dists.py build --out dist
.venv-dist/bin/python -I scripts/dists.py check dist --python python3.11 --python python3.13
```

`check` runs `twine check --strict`, compares the files in the sdist and the wheel with
`git ls-files store_release`, and runs the wheel alone in a new environment for each
`--python` (`store-release --help`, `--version`, and `init` writing the templates).

The `workflows` CI job also runs `actionlint` 1.7.12 on `.github/workflows/` and on every
example's and template's callers. No test needs a store account, credentials or the
network: tests generate the keys and files they need, and fake App Store Connect, Google
Play and GitHub.

## Rules for a change

- **Pull requests only.** `main` changes through pull requests, and each must pass CI:
  `test (stdlib)`, `test (strict)`, `test (macos)`, `lint`, `package`, `workflows`, CodeQL
  (`Analyze (python)`, `Analyze (actions)`) and dependency review. The branch must be up to
  date with `main`, and review threads resolved, before it merges.
- **Tests with every change**, in the suite that matches it (`tests/unit`, `tests/apps`,
  `tests/workflows`); a bug fix comes with the test that would have caught it.
- **A CHANGELOG entry** under `## [Unreleased]`, with the change's Impact
  (`checks-only`, `validation`, `release-path` or `config-breaking`) and any action apps
  must take ([release process](docs/release-process.md)).
- **Keep behaviour.** A change that alters what reaches a store needs its own config key or
  switch, off by default, unless it fixes a bug. Config keys are additive: a new key is
  optional with a default that keeps today's behaviour, and keys are never renamed.
- **Document what you add.** A new config key needs its row in
  [docs/config.md](docs/config.md), a new switch its row in
  [docs/switches.md](docs/switches.md), a new secret its rows in
  [docs/secrets.md](docs/secrets.md); `tests/workflows/test_docs.py` fails otherwise.
- **No app-specific values** in `store_release/` (outside the ported flows),
  `.github/workflows/` or `.github/actions/`: names, bundle ids, team or account ids,
  certificates and e-mail addresses belong in an app's `store/store-release.toml`. A test
  enforces this.
- **The ported flows are verbatim.** `store_release/flows/<app>/` is an app's own scripts
  with only the port's edits, and `tests/parity/upstream_*` are the verbatim copies, hashed
  in `MANIFEST.sha256`. Never edit either by hand; see [flows.md](docs/flows.md#changing-a-flow).
- **No credentials** in the repository, ever: no keys, certificates, keystores,
  service-account JSON or tokens, not even expired ones.
- **Pins.** Third-party actions are pinned by full commit SHA with a `# vX.Y.Z` comment,
  with the same pin for an action in every file (a test enforces it). Python dependencies
  are pinned by hash (`pip-compile --generate-hashes`, pip-tools 7.6.2, Python 3.13). Each
  of the package's extras in `pyproject.toml` allows one major version, `>=<floor>,<<next
  major>`, and must hold the version `requirements.in` or `requirements-checks.in` pins (a
  test enforces it): a Dependabot bump within the major leaves `pyproject.toml` alone, and
  a new major moves the floor to the new lock and the cap past it.
- **Permissions.** Every job declares its `permissions`, and the rendered callers do too;
  expressions reach shell only through `env:` (tests enforce both).
- **Commits** say what changed and why in plain words. By contributing you agree that your
  contribution is licensed under the [MIT License](LICENSE).

## Releases

Releases are cut by the maintainer through the dispatch-only Release workflow
([release process](docs/release-process.md)); a pull request never creates a tag.
