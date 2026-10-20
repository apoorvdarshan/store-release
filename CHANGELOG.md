# Changelog

All notable changes to store-release. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[SemVer](https://semver.org/) ([release process](docs/release-process.md)). Every
release section has an `Impact:` line: `checks-only`, `validation`, `release-path`
or `config-breaking`.

## [Unreleased]

Impact: release-path (the first release)

### Added

- The `store_release` package: Fud AI's store tooling (fud-ai
  `a99591908d5f737405ca7251436d4669e32c90c6`) with its constants moved into
  `store/store-release.toml` (schema 1), plus config validation, per-job sparse
  checkout paths, job-boundary checks, caller rendering and checks, yanked versions,
  the hand-written App Store Connect client and a read-only store plan.
- Ten reusable workflows (`ios-prepare`, `ios-build`, `ios-wait-for-build`,
  `ios-app-store-connect`, `ios-github-release`, `ios-store-plan`, `android-gates`,
  `android-publish`, `android-store-plan`, `checks`) and the `job-setup` action.
- The 215 ported fud-ai store tests, new unit tests, fixture-app tests, a
  differential parity suite against the vendored fud-ai scripts, workflow tests,
  and docs tests.
- Fud AI's config and rendered callers in `examples/fud-ai/`.
- CI (`test (stdlib)`, `test (strict)`, `test (macos)`, `lint`, `workflows`, and the
  advisory `canary (fud-ai)` against fud-ai's real store files), CodeQL
  (`Analyze (python)`, `Analyze (actions)`), dependency review, Dependabot, the
  dispatch-only Release workflow (`scripts/release_check.py`) and the re-run probe.
- Docs: config, switches, secrets, workflows, re-runs, store plan, verification,
  release process, yanked versions, provenance and roadmap.

### App actions required

Fud AI's migration (one PR in fud-ai, validated by dispatches before merge):

- add `store/store-release.toml` (`examples/fud-ai/store-release.toml`);
- replace `ios-release.yml` and `android-release.yml` with the rendered callers at
  the release SHA, add `store-checks.yml`, delete `store-automation.yml`;
- move `scripts/store/validate_catalog.py` unchanged to `scripts/ci/check_catalog.py`;
  delete the rest of `scripts/store/`, `scripts/release_notes.py`,
  `requirements-store.in/.txt`, `store/gates.env.example` and the unused
  `store/metadata` placeholders;
- in `.github/dependabot.yml`, delete the root pip entry and add a `store-release`
  group before the catch-all github-actions group;
- trim `store/README.md` to Fud AI facts, linking to these docs.

No switch, secret or variable changes.

### Verification

Recorded when the release is cut: CI, the canary, the re-run probe
([docs/rerun.md](docs/rerun.md)) and Fud AI's validation dispatches with their
store-plan results.
