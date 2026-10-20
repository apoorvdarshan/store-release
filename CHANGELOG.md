# Changelog

All notable changes to store-release. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[SemVer](https://semver.org/). Every release section has an `Impact:` line:
`checks-only`, `validation`, `release-path` or `config-breaking`.

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
  differential parity suite against the vendored fud-ai scripts, and workflow tests.
- Fud AI's config and rendered callers in `examples/fud-ai/`.
- CI (`test (stdlib)`, `test (strict)`, `test (macos)`, `lint`, `workflows`), CodeQL
  (`Analyze (python)`, `Analyze (actions)`), dependency review, Dependabot, and the
  re-run probe's inner workflow.
